"""Durable job store over the ``jobs`` / ``job_runs`` tables.

All timestamps are stored as UTC ISO-8601 strings with a fixed microsecond
format so that SQL string comparison orders them correctly. ``next_run_at`` is
both the due time and, while a run is in flight, its *lease*: the claiming
UPDATE moves it forward, so a crashed process leaves the job due again after
the lease expires and the open run is resumed under the same idempotency key.
"""

from __future__ import annotations

import json
import secrets
import weakref
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..domain.errors import ErrorCode, MailError
from ..storage.db import Database

ACTIVE = "active"
PAUSED = "paused"
DONE = "done"
CANCELLED = "cancelled"
FAILED = "failed"
JOB_STATUSES = frozenset({ACTIVE, PAUSED, DONE, CANCELLED, FAILED})

MISSED_POLICIES = frozenset({"run_once", "skip", "run_all"})


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    """Canonical storage form: UTC, microseconds, ``+00:00``."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("naive datetime; attach a timezone first")
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def new_job_id() -> str:
    return "job_" + secrets.token_urlsafe(12)


@dataclass
class Job:
    id: str
    type: str
    client_id: str
    account: str
    status: str
    spec: dict[str, Any]
    next_run_at: datetime | None
    timezone: str | None
    missed_run_policy: str
    last_run_at: datetime | None
    last_result: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    _next_raw: str | None = field(default=None, repr=False)  # exact stored value (CAS token)

    def public(self) -> dict[str, Any]:
        """Caller-facing view. Specs never carry secrets; outgoing bodies are summarized."""
        spec = dict(self.spec)
        send = spec.get("send")
        if isinstance(send, dict):
            spec["send"] = {
                "to": [a.get("email") for a in send.get("to", [])],
                "cc": [a.get("email") for a in send.get("cc", [])],
                "bcc_count": len(send.get("bcc", [])),
                "subject": send.get("subject"),
                "attachments": [
                    {"filename": a.get("filename"), "sha256": a.get("sha256"),
                     "size": a.get("size")} for a in send.get("attachment_manifest", [])],
                "content_sha256": spec.get("content_sha256"),
            }
        return {
            "job_id": self.id, "type": self.type, "client_id": self.client_id,
            "account": self.account, "status": self.status, "spec": spec,
            "next_run_at": iso(self.next_run_at) if self.status == ACTIVE else None,
            "timezone": self.timezone, "missed_run_policy": self.missed_run_policy,
            "last_run_at": iso(self.last_run_at), "last_result": self.last_result,
            "created_at": iso(self.created_at),
            "note": spec.get("label"),
        }


@dataclass
class JobRun:
    id: int
    job_id: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    operation_id: str | None
    detail: dict[str, Any]

    def public(self) -> dict[str, Any]:
        return {"run_id": self.id, "job_id": self.job_id, "started_at": iso(self.started_at),
                "finished_at": iso(self.finished_at), "status": self.status,
                "operation_id": self.operation_id, **self.detail}


def _job(r: Any) -> Job:
    return Job(
        id=r["id"], type=r["type"], client_id=r["client_id"], account=r["account"],
        status=r["status"], spec=json.loads(r["spec_json"]),
        next_run_at=parse_dt(r["next_run_at"]), timezone=r["timezone"],
        missed_run_policy=r["missed_run_policy"], last_run_at=parse_dt(r["last_run_at"]),
        last_result=json.loads(r["last_result_json"]) if r["last_result_json"] else None,
        created_at=parse_dt(r["created_at"]),  # type: ignore[arg-type]
        updated_at=parse_dt(r["updated_at"]),  # type: ignore[arg-type]
        _next_raw=r["next_run_at"],
    )


def _run(r: Any) -> JobRun:
    try:
        detail = json.loads(r["detail"]) if r["detail"] else {}
    except ValueError:
        detail = {"message": r["detail"]}
    return JobRun(
        id=r["id"], job_id=r["job_id"], started_at=parse_dt(r["started_at"]),  # type: ignore[arg-type]
        finished_at=parse_dt(r["finished_at"]), status=r["status"],
        operation_id=r["operation_id"], detail=detail,
    )


_ENSURED: weakref.WeakSet[Database] = weakref.WeakSet()


class JobStore:
    def __init__(self, db: Database) -> None:
        self.db = db
        if db not in _ENSURED:
            db.migrate()  # idempotent; guarantees this package's tables exist
            _ENSURED.add(db)

    # ------------------------------------------------------------------ CRUD
    def create(self, *, job_type: str, client_id: str, account: str, spec: dict[str, Any],
               next_run_at: datetime | None, timezone: str | None = None,
               missed_run_policy: str = "run_once", job_id: str | None = None,
               status: str = ACTIVE, now: datetime | None = None) -> Job:
        """Insert a job; an existing id is returned unchanged (idempotent registration)."""
        if missed_run_policy not in MISSED_POLICIES:
            raise MailError(ErrorCode.INVALID_REQUEST,
                            f"missed_run_policy must be one of {sorted(MISSED_POLICIES)}")
        job_id = job_id or new_job_id()
        stamp = iso(now or utcnow())
        with self.db.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO jobs (id, type, client_id, account, status, spec_json, "
                "next_run_at, timezone, missed_run_policy, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, job_type, client_id, account, status, json.dumps(spec, default=str),
                 iso(next_run_at), timezone, missed_run_policy, stamp, stamp))
        job = self.get(job_id)
        assert job is not None
        return job

    def get(self, job_id: str) -> Job | None:
        rows = self.db.query("SELECT * FROM jobs WHERE id=?", (job_id,))
        return _job(rows[0]) if rows else None

    def find(self, *, client_id: str | None = None, job_type: str | None = None,
             status: str | None = None, account: str | None = None, limit: int = 100
             ) -> list[Job]:
        sql = "SELECT * FROM jobs WHERE 1=1"
        params: list[Any] = []
        for col, val in (("client_id", client_id), ("type", job_type), ("status", status),
                         ("account", account)):
            if val is not None:
                sql += f" AND {col}=?"
                params.append(val)
        sql += " ORDER BY created_at DESC, id LIMIT ?"
        params.append(min(max(limit, 1), 500))
        return [_job(r) for r in self.db.query(sql, tuple(params))]

    def update(self, job_id: str, *, spec: dict[str, Any] | None = None,
               status: str | None = None, timezone: str | None = None,
               missed_run_policy: str | None = None, next_run_at: datetime | None | str = "keep",
               now: datetime | None = None) -> None:
        sets, params = ["updated_at=?"], [iso(now or utcnow())]
        if spec is not None:
            sets.append("spec_json=?")
            params.append(json.dumps(spec, default=str))
        if status is not None:
            sets.append("status=?")
            params.append(status)
        if timezone is not None:
            sets.append("timezone=?")
            params.append(timezone)
        if missed_run_policy is not None:
            sets.append("missed_run_policy=?")
            params.append(missed_run_policy)
        if next_run_at != "keep":
            sets.append("next_run_at=?")
            params.append(iso(next_run_at) if isinstance(next_run_at, datetime) else None)
        params.append(job_id)
        with self.db.tx() as c:
            c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", tuple(params))

    # ------------------------------------------------------------------ scheduling
    def due(self, now: datetime, limit: int = 50) -> list[Job]:
        rows = self.db.query(
            "SELECT * FROM jobs WHERE status='active' AND next_run_at IS NOT NULL "
            "AND next_run_at <= ? ORDER BY next_run_at LIMIT ?", (iso(now), limit))
        return [_job(r) for r in rows]

    def claim(self, job: Job, lease_until: datetime, now: datetime,
              scheduled_for: datetime) -> tuple[JobRun, bool] | None:
        """Atomically take the job for one run (compare-and-set on ``next_run_at``).

        Returns ``None`` when another thread or process claimed it first. If an
        earlier run is still ``running`` (a crash left it open) it is *resumed*
        so the same scheduled time and idempotency key are used again; the boolean
        says whether this run is such a resumption.
        """
        with self.db.tx() as c:
            cur = c.execute(
                "UPDATE jobs SET next_run_at=?, updated_at=? WHERE id=? AND status='active' "
                "AND next_run_at=?", (iso(lease_until), iso(now), job.id, job._next_raw))
            if cur.rowcount != 1:
                return None
            open_run = c.execute(
                "SELECT * FROM job_runs WHERE job_id=? AND status='running' "
                "ORDER BY id DESC LIMIT 1", (job.id,)).fetchone()
            if open_run is not None:
                return _run(open_run), True
            cur = c.execute(
                "INSERT INTO job_runs (job_id, started_at, status, detail) VALUES (?,?,?,?)",
                (job.id, iso(now), "running",
                 json.dumps({"scheduled_for": iso(scheduled_for)})))
            row = c.execute("SELECT * FROM job_runs WHERE id=?", (cur.lastrowid,)).fetchone()
        return _run(row), False

    def finish_run(self, job: Job, run: JobRun, *, status: str, now: datetime,
                   operation_id: str | None, detail: dict[str, Any], job_status: str,
                   next_run_at: datetime | None, spec: dict[str, Any] | None = None) -> None:
        """Record the run and advance the job in one transaction."""
        merged = {**run.detail, **detail}
        with self.db.tx() as c:
            c.execute("UPDATE job_runs SET finished_at=?, status=?, operation_id=?, detail=? "
                      "WHERE id=?", (iso(now), status, operation_id,
                                     json.dumps(merged, default=str), run.id))
            sets = ["status=?", "next_run_at=?", "last_run_at=?", "last_result_json=?",
                    "updated_at=?"]
            params: list[Any] = [job_status, iso(next_run_at), iso(now),
                                 json.dumps({"status": status, "operation_id": operation_id,
                                             **{k: v for k, v in detail.items()
                                                if k in ("message", "error", "scheduled_for")}},
                                            default=str), iso(now)]
            if spec is not None:
                sets.append("spec_json=?")
                params.append(json.dumps(spec, default=str))
            params.append(job.id)
            # A cancel/pause that landed while the run was in flight wins.
            c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=? AND status='active'",
                      tuple(params))

    def record_run(self, job_id: str, *, status: str, now: datetime,
                   operation_id: str | None = None, detail: dict[str, Any] | None = None
                   ) -> int:
        """Insert a completed run (manual execution, trigger batches)."""
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO job_runs (job_id, started_at, finished_at, status, operation_id, "
                "detail) VALUES (?,?,?,?,?,?)",
                (job_id, iso(now), iso(now), status, operation_id,
                 json.dumps(detail or {}, default=str)))
            c.execute("UPDATE jobs SET last_run_at=?, last_result_json=?, updated_at=? "
                      "WHERE id=?", (iso(now), json.dumps({"status": status,
                                                           "operation_id": operation_id}),
                                     iso(now), job_id))
            return int(cur.lastrowid or 0)

    def runs(self, job_id: str, limit: int = 20) -> list[JobRun]:
        rows = self.db.query("SELECT * FROM job_runs WHERE job_id=? ORDER BY id DESC LIMIT ?",
                             (job_id, min(max(limit, 1), 200)))
        return [_run(r) for r in rows]

    # ------------------------------------------------------------------ cursors
    def cursor(self, name: str) -> int | None:
        rows = self.db.query("SELECT value FROM job_cursors WHERE name=?", (name,))
        return int(rows[0][0]) if rows else None

    def set_cursor(self, name: str, value: int) -> None:
        with self.db.tx() as c:
            c.execute("INSERT INTO job_cursors(name, value) VALUES (?,?) "
                      "ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, value))

    def max_event_seq(self) -> int:
        rows = self.db.query("SELECT COALESCE(MAX(seq), 0) FROM events")
        return int(rows[0][0])
