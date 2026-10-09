"""The application kernel shared by every entry point.

All authorization lives here so MCP tools, resources, CLI, UI and jobs cannot
bypass policy. Flow for a write::

    request -> policy.evaluate
      Deny  -> MailError(policy_denied | constraint_violation | ...)
      Ask   -> journal(pending) -> optional trusted reviewer -> approval_pending outcome
      Allow -> journal(executing) -> executor -> journal(final)

    resume(op_id) -> caller-scoped read -> recheck policy (revocation is immediate)
                  -> atomic claim (approved + same digest + unexpired) -> executor

Executors are registered per operation kind and receive only the stored
request, so resume never accepts replacement arguments.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import ServiceConfig
from ..domain.errors import ErrorCode, MailError
from ..domain.families import OperationFamily, family_of, register_kind
from ..domain.models import ItemResult, OperationOutcome, OperationStatus
from ..domain.requests import CallerContext, OperationRequest
from ..policy import engine
from ..policy.model import Action, PolicyConfig
from ..storage.db import Database
from ..storage.journal import Journal, OperationRecord

if TYPE_CHECKING:
    from ..bridge.ports import MailStore, MailTransport

log = logging.getLogger(__name__)


@dataclass
class ExecResult:
    status: OperationStatus = OperationStatus.SUCCEEDED
    result: dict[str, Any] = field(default_factory=dict)
    items: list[ItemResult] | None = None
    prior_states: list[dict[str, Any] | None] | None = None


Executor = Callable[["MailApp", OperationRecord], ExecResult]
# Returns True (approve), False (deny) or None (no decision; stays pending).
Reviewer = Callable[[OperationRecord], bool | None]

_EXECUTORS: dict[str, Executor] = {}


def executor(kind: str, family: OperationFamily) -> Callable[[Executor], Executor]:
    """Register an operation kind, its policy family, and its executor."""
    register_kind(kind, family)

    def deco(fn: Executor) -> Executor:
        _EXECUTORS[kind] = fn
        return fn

    return deco


def get_executor(kind: str) -> Executor:
    fn = _EXECUTORS.get(kind)
    if fn is None:
        raise MailError(ErrorCode.UNSUPPORTED, f"operation {kind!r} is not available")
    return fn


StoreFactory = Callable[[str], "MailStore"]
TransportFactory = Callable[[str], "MailTransport"]


class MailApp:
    """One instance per process. Thread-safe."""

    def __init__(
        self,
        config: ServiceConfig,
        policy: PolicyConfig,
        db: Database,
        store_factory: StoreFactory,
        transport_factory: TransportFactory,
        policy_loader: Callable[[], PolicyConfig] | None = None,
    ) -> None:
        self.config = config
        self._policy = policy
        self._policy_loader = policy_loader
        self.db = db
        self.journal = Journal(db)
        self._store_factory = store_factory
        self._transport_factory = transport_factory
        self._stores: dict[str, MailStore] = {}
        self._transports: dict[str, MailTransport] = {}
        self._lock = threading.RLock()
        # Ensure all service modules registered their executors.
        from . import registry  # noqa: F401

    # ------------------------------------------------------------ resources
    @property
    def policy(self) -> PolicyConfig:
        """Policy is re-read on each access when a loader is configured so
        owner edits and revocations take effect immediately."""
        if self._policy_loader is not None:
            try:
                self._policy = self._policy_loader()
            except Exception:  # noqa: BLE001 - keep last good policy on parse errors
                log.exception("failed to reload policy; keeping previous policy")
        return self._policy

    def set_policy(self, policy: PolicyConfig) -> None:
        self._policy = policy

    def store(self, account: str) -> MailStore:
        self.config.account(account)  # validates account exists
        with self._lock:
            if account not in self._stores:
                self._stores[account] = self._store_factory(account)
            return self._stores[account]

    def transport(self, account: str) -> MailTransport:
        self.config.account(account)
        with self._lock:
            if account not in self._transports:
                self._transports[account] = self._transport_factory(account)
            return self._transports[account]

    def data_dir(self) -> Path:
        return self.config.resolved_data_dir()

    def close(self) -> None:
        with self._lock:
            for s in self._stores.values():
                try:
                    s.close()
                except Exception:  # noqa: BLE001
                    log.debug("error closing store", exc_info=True)
            self._stores.clear()

    # ------------------------------------------------------------ authorization
    def _sends_today(self, req: OperationRequest, caller: CallerContext) -> int:
        if req.family is not OperationFamily.SEND:
            return 0
        return self.journal.sends_since(req.account, datetime.now(UTC) - timedelta(days=1))

    def decide(self, caller: CallerContext, req: OperationRequest) -> engine.Decision:
        expected = family_of(req.kind)
        if expected is None:
            raise MailError(ErrorCode.UNSUPPORTED, f"operation {req.kind!r} is not classified")
        if expected != req.family:
            raise MailError(ErrorCode.INTERNAL, "operation family mismatch", kind=req.kind)
        return engine.evaluate(self.policy, caller, req, self._sends_today(req, caller))

    def _raise_denied(self, d: engine.Decision, req: OperationRequest) -> None:
        code = ErrorCode(d.code or "policy_denied")
        msg = d.violation or f"{req.family.value} is denied by policy"
        raise MailError(code, msg, kind=req.kind, family=req.family.value, reasons=d.reasons)

    def authorize_read(self, caller: CallerContext, account: str,
                       mailboxes: list[str] | None = None, kind: str = "messages.read") -> None:
        """Reads (tools *and* resources) pass the same policy and identity checks."""
        req = OperationRequest(kind=kind, family=OperationFamily.READ, account=account,
                               mailboxes=mailboxes or [])
        d = engine.evaluate(self.policy, caller, req)
        if d.action is Action.DENY:
            self._raise_denied(d, req)
        if d.action is Action.ASK:
            raise MailError(ErrorCode.POLICY_DENIED,
                            "reading is set to Ask; read approvals are not supported, "
                            "set reading to Allow or Deny", kind=kind)

    # ------------------------------------------------------------ execution
    def run(self, caller: CallerContext, req: OperationRequest,
            reviewer: Reviewer | None = None) -> OperationOutcome:
        if req.idempotency_key:
            prior = self.journal.find_idempotent(caller.client_id, req.idempotency_key)
            if prior is not None:
                if prior.digest != req.digest():
                    raise MailError(ErrorCode.CONFLICT,
                                    "idempotency key reused with a different request")
                return self._outcome(prior)
        d = self.decide(caller, req)
        if d.action is Action.DENY:
            self._raise_denied(d, req)
        get_executor(req.kind)  # fail before journaling if unavailable
        if d.action is Action.ASK:
            rec = self.journal.create(caller, req, OperationStatus.PENDING,
                                      ttl_seconds=self.policy.approval_ttl_seconds,
                                      reasons=d.reasons)
            if reviewer is not None:
                decision = None
                try:
                    decision = reviewer(rec)
                except Exception:  # noqa: BLE001 - reviewer failure leaves it pending
                    log.warning("review channel failed; operation stays pending", exc_info=True)
                if decision is not None:
                    self.journal.decide(rec.id, decision, f"review:{caller.client_id}")
                    if decision:
                        return self.resume(caller, rec.id)
                    rec = self.journal.get(rec.id) or rec
            return self._outcome(rec)
        rec = self.journal.create(caller, req, OperationStatus.EXECUTING, reasons=d.reasons)
        return self._execute(rec)

    def resume(self, caller: CallerContext, op_id: str) -> OperationOutcome:
        rec = self.journal.get_for_caller(caller, op_id)
        if rec.status in (OperationStatus.PENDING,):
            return self._outcome(rec)
        if rec.status is not OperationStatus.APPROVED:
            return self._outcome(rec)  # terminal or executing: report, never re-run
        # Policy recheck: revocation, pauses and new Deny rules take effect now.
        d = self.decide(caller if not caller.is_owner else self._original_caller(rec), rec.request)
        if d.action is Action.DENY:
            self.journal.cancel(rec.id)
            self._raise_denied(d, rec.request)
        if rec.request.digest() != rec.digest:
            self.journal.cancel(rec.id)
            raise MailError(ErrorCode.APPROVAL_INVALID, "stored request changed; request again")
        if not self.journal.claim(rec.id, rec.digest):
            cur = self.journal.get(rec.id) or rec
            return self._outcome(cur)
        claimed = self.journal.get(rec.id)
        assert claimed is not None
        return self._execute(claimed)

    def _original_caller(self, rec: OperationRecord) -> CallerContext:
        from ..domain.requests import Transport

        return CallerContext(client_id=rec.client_id, transport=Transport(rec.transport))

    def _execute(self, rec: OperationRecord) -> OperationOutcome:
        fn = get_executor(rec.kind)
        try:
            res = fn(self, rec)
        except MailError as e:
            status = (OperationStatus.DELIVERY_UNKNOWN if e.code is ErrorCode.DELIVERY_UNKNOWN
                      else OperationStatus.FAILED)
            done = self.journal.finish(rec.id, status, error=e.to_dict())
            return self._outcome(done)
        except Exception as e:  # noqa: BLE001 - journal every failure, bounded message
            log.exception("operation %s failed", rec.id)
            err = MailError(ErrorCode.INTERNAL, type(e).__name__).to_dict()
            done = self.journal.finish(rec.id, OperationStatus.FAILED, error=err)
            return self._outcome(done)
        if res.items is not None:
            self.journal.record_items(rec.id, res.items, res.prior_states)
        done = self.journal.finish(rec.id, res.status, result=res.result)
        return self._outcome(done)

    def _outcome(self, rec: OperationRecord) -> OperationOutcome:
        items = [i for i, _ in self.journal.items(rec.id)] or None
        return rec.outcome(items)

    # ------------------------------------------------------------ status / owner
    def status(self, caller: CallerContext, op_id: str) -> OperationOutcome:
        return self._outcome(self.journal.get_for_caller(caller, op_id))

    def cancel(self, caller: CallerContext, op_id: str) -> OperationOutcome:
        rec = self.journal.get_for_caller(caller, op_id)
        self.journal.cancel(rec.id)
        return self._outcome(self.journal.get(rec.id) or rec)

    def approve(self, owner: CallerContext, op_id: str, approve: bool = True,
                execute: bool = False) -> OperationOutcome:
        """Owner decision. Operation IDs and client labels never grant authority."""
        if not owner.is_owner:
            raise MailError(ErrorCode.POLICY_DENIED, "only the owner can review operations")
        rec = self.journal.decide(op_id, approve, f"owner:{owner.transport.value}")
        if approve and execute:
            return self.resume(owner, op_id)
        return self._outcome(rec)
