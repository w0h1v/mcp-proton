"""Durable operation journal and approval state machine.

State machine::

    pending --approve--> approved --claim--> executing --> succeeded
       |  \\--deny--> denied                         \\--> partially_succeeded
       |   \\--expire--> expired                      \\--> failed
       \\--cancel--> cancelled                        \\--> delivery_unknown
    (Allow) created directly as executing.

``claim`` is an atomic compare-and-set so concurrent resumes cannot execute an
approved payload twice. Approval consumption and execution results are durable.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..domain.errors import ErrorCode, MailError
from ..domain.models import ItemResult, OperationOutcome, OperationStatus
from ..domain.requests import CallerContext, OperationRequest
from .db import Database


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def new_operation_id() -> str:
    return "op_" + secrets.token_urlsafe(16)


@dataclass
class OperationRecord:
    id: str
    kind: str
    family: str
    client_id: str
    transport: str
    account: str
    status: OperationStatus
    digest: str
    request: OperationRequest
    summary: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    decided_at: datetime | None
    decided_by: str | None
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    message_id: str | None
    policy_reasons: list[str]
    idempotency_key: str | None

    def outcome(self, items: list[ItemResult] | None = None) -> OperationOutcome:
        return OperationOutcome(
            status=self.status,
            operation_id=self.id,
            kind=self.kind,
            summary=self.summary,
            expires_at=self.expires_at if self.status is OperationStatus.PENDING else None,
            result=self.result,
            items=items,
            error=self.error,
        )


class Journal:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------ creation
    def create(self, caller: CallerContext, req: OperationRequest, status: OperationStatus,
               ttl_seconds: int | None = None, reasons: list[str] | None = None,
               message_id: str | None = None) -> OperationRecord:
        now = _now()
        op_id = new_operation_id()
        expires = now + timedelta(seconds=ttl_seconds) if ttl_seconds else None
        with self.db.tx() as c:
            c.execute(
                """INSERT INTO operations (id, kind, family, client_id, transport, account, status,
                   digest, request_json, summary, idempotency_key, created_at, updated_at,
                   expires_at, started_at, message_id, policy_reasons)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    op_id, req.kind, req.family.value, caller.client_id, caller.transport.value,
                    req.account, status.value, req.digest(), req.model_dump_json(by_alias=True),
                    req.summary, req.idempotency_key, _iso(now), _iso(now), _iso(expires),
                    _iso(now) if status is OperationStatus.EXECUTING else None, message_id,
                    json.dumps(reasons or []),
                ),
            )
        rec = self.get(op_id)
        assert rec is not None
        return rec

    def find_idempotent(self, client_id: str, key: str) -> OperationRecord | None:
        rows = self.db.query(
            "SELECT * FROM operations WHERE client_id=? AND idempotency_key=?", (client_id, key)
        )
        return self._row(rows[0]) if rows else None

    # ------------------------------------------------------------ reads
    def get(self, op_id: str) -> OperationRecord | None:
        rows = self.db.query("SELECT * FROM operations WHERE id=?", (op_id,))
        if not rows:
            return None
        rec = self._row(rows[0])
        if rec.status is OperationStatus.PENDING and rec.expires_at and rec.expires_at <= _now():
            self._transition(op_id, {OperationStatus.PENDING, OperationStatus.APPROVED},
                             OperationStatus.EXPIRED)
            return self.get(op_id)
        if rec.status is OperationStatus.APPROVED and rec.expires_at and rec.expires_at <= _now():
            self._transition(op_id, {OperationStatus.APPROVED}, OperationStatus.EXPIRED)
            return self.get(op_id)
        return rec

    def get_for_caller(self, caller: CallerContext, op_id: str) -> OperationRecord:
        """Caller-scoped read: clients see only their own operations; the owner sees all."""
        rec = self.get(op_id)
        if rec is None or (not caller.is_owner and rec.client_id != caller.client_id):
            raise MailError(ErrorCode.NOT_FOUND, "operation not found", operation_id=op_id)
        return rec

    def list(self, *, status: OperationStatus | None = None, client_id: str | None = None,
             account: str | None = None, limit: int = 50, before: str | None = None
             ) -> list[OperationRecord]:
        sql, params = "SELECT * FROM operations WHERE 1=1", []
        if status:
            sql += " AND status=?"
            params.append(status.value)
        if client_id:
            sql += " AND client_id=?"
            params.append(client_id)
        if account:
            sql += " AND account=?"
            params.append(account)
        if before:
            sql += " AND created_at < (SELECT created_at FROM operations WHERE id=?)"
            params.append(before)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(min(max(limit, 1), 500))
        return [self._row(r) for r in self.db.query(sql, tuple(params))]

    def expire_due(self) -> int:
        now = _iso(_now())
        with self.db.tx() as c:
            cur = c.execute(
                "UPDATE operations SET status='expired', updated_at=? WHERE status IN "
                "('pending','approved') AND expires_at IS NOT NULL AND expires_at <= ?",
                (now, now),
            )
            return cur.rowcount

    def sends_since(self, account: str, since: datetime, client_id: str | None = None) -> int:
        sql = ("SELECT COUNT(*) FROM operations WHERE family='send' AND account=? AND "
               "created_at >= ? AND status IN ('executing','succeeded','partially_succeeded',"
               "'delivery_unknown')")
        params: list[Any] = [account, _iso(since)]
        if client_id:
            sql += " AND client_id=?"
            params.append(client_id)
        return int(self.db.query(sql, tuple(params))[0][0])

    # ------------------------------------------------------------ transitions
    def _transition(self, op_id: str, from_states: set[OperationStatus], to: OperationStatus,
                    **fields: Any) -> bool:
        sets = ["status=?", "updated_at=?"]
        params: list[Any] = [to.value, _iso(_now())]
        for k, v in fields.items():
            sets.append(f"{k}=?")
            params.append(v)
        placeholders = ",".join("?" for _ in from_states)
        params.extend([op_id, *[s.value for s in from_states]])
        with self.db.tx() as c:
            cur = c.execute(
                f"UPDATE operations SET {', '.join(sets)} WHERE id=? AND status IN ({placeholders})",
                tuple(params),
            )
            return cur.rowcount == 1

    def decide(self, op_id: str, approve: bool, decided_by: str) -> OperationRecord:
        rec = self.get(op_id)
        if rec is None:
            raise MailError(ErrorCode.NOT_FOUND, "operation not found", operation_id=op_id)
        if rec.status is not OperationStatus.PENDING:
            raise MailError(ErrorCode.APPROVAL_INVALID, f"operation is {rec.status.value}",
                            operation_id=op_id)
        to = OperationStatus.APPROVED if approve else OperationStatus.DENIED
        ok = self._transition(op_id, {OperationStatus.PENDING}, to,
                              decided_at=_iso(_now()), decided_by=decided_by)
        if not ok:
            raise MailError(ErrorCode.CONFLICT, "operation changed concurrently", operation_id=op_id)
        out = self.get(op_id)
        assert out is not None
        return out

    def cancel(self, op_id: str) -> bool:
        return self._transition(op_id, {OperationStatus.PENDING, OperationStatus.APPROVED},
                                OperationStatus.CANCELLED)

    def claim(self, op_id: str, digest: str) -> bool:
        """Atomically consume an approval. Fails if not approved, expired, or changed."""
        now = _iso(_now())
        with self.db.tx() as c:
            cur = c.execute(
                "UPDATE operations SET status='executing', started_at=?, updated_at=? "
                "WHERE id=? AND status='approved' AND digest=? AND "
                "(expires_at IS NULL OR expires_at > ?)",
                (now, now, op_id, digest, now),
            )
            return cur.rowcount == 1

    def finish(self, op_id: str, status: OperationStatus, result: dict[str, Any] | None = None,
               error: dict[str, Any] | None = None) -> OperationRecord:
        self._transition(
            op_id, {OperationStatus.EXECUTING}, status,
            finished_at=_iso(_now()),
            result_json=json.dumps(result, default=str) if result is not None else None,
            error_json=json.dumps(error, default=str) if error is not None else None,
        )
        rec = self.get(op_id)
        assert rec is not None
        return rec

    def set_message_id(self, op_id: str, message_id: str) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE operations SET message_id=? WHERE id=?", (message_id, op_id))

    def record_items(self, op_id: str, items: list[ItemResult],
                     prior_states: list[dict[str, Any] | None] | None = None) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM operation_items WHERE operation_id=?", (op_id,))
            for i, it in enumerate(items):
                prior = prior_states[i] if prior_states and i < len(prior_states) else None
                c.execute(
                    "INSERT INTO operation_items VALUES (?,?,?,?,?,?,?)",
                    (op_id, i, it.target, it.status, it.detail, it.new_handle,
                     json.dumps(prior) if prior is not None else None),
                )

    def items(self, op_id: str) -> list[tuple[ItemResult, dict[str, Any] | None]]:
        rows = self.db.query(
            "SELECT * FROM operation_items WHERE operation_id=? ORDER BY seq", (op_id,)
        )
        return [
            (
                ItemResult(target=r["target"], status=r["status"], detail=r["detail"],
                           new_handle=r["new_handle"]),
                json.loads(r["prior_state"]) if r["prior_state"] else None,
            )
            for r in rows
        ]

    def purge(self, older_than: datetime) -> int:
        """Retention: delete terminal records older than the cutoff."""
        with self.db.tx() as c:
            cur = c.execute(
                "DELETE FROM operations WHERE finished_at IS NOT NULL AND finished_at < ? OR "
                "(status IN ('denied','expired','cancelled') AND updated_at < ?)",
                (_iso(older_than), _iso(older_than)),
            )
            return cur.rowcount

    # ------------------------------------------------------------ helpers
    def _row(self, r: Any) -> OperationRecord:
        return OperationRecord(
            id=r["id"], kind=r["kind"], family=r["family"], client_id=r["client_id"],
            transport=r["transport"], account=r["account"], status=OperationStatus(r["status"]),
            digest=r["digest"], request=OperationRequest.model_validate_json(r["request_json"]),
            summary=r["summary"], created_at=_dt(r["created_at"]),  # type: ignore[arg-type]
            updated_at=_dt(r["updated_at"]),  # type: ignore[arg-type]
            expires_at=_dt(r["expires_at"]), decided_at=_dt(r["decided_at"]),
            decided_by=r["decided_by"],
            result=json.loads(r["result_json"]) if r["result_json"] else None,
            error=json.loads(r["error_json"]) if r["error_json"] else None,
            message_id=r["message_id"],
            policy_reasons=json.loads(r["policy_reasons"] or "[]"),
            idempotency_key=r["idempotency_key"],
        )
