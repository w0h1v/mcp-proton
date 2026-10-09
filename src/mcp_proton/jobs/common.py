"""Shared plumbing for the automation layer.

* Registration, edit and cancel are **writes of family AUTOMATION** and go through
  ``MailApp.run``; the executors below create/modify the job rows, so under an
  Assistant preset (Ask) nothing exists until the owner approves, and a Reader
  preset denies outright.
* A job records the ``client_id`` of its creator. When it fires, the mail action
  is submitted through ``MailApp.run`` as that client with ``Transport.JOB``, so
  revocation, paused accounts and newly denied families apply at fire time.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..domain.errors import ErrorCode, MailError, invalid, not_found
from ..domain.families import OperationFamily
from ..domain.models import MessageHandle, OperationOutcome, OperationStatus
from ..domain.requests import CallerContext, OperationRequest, Transport
from ..services.core import ExecResult, MailApp, executor
from ..storage.journal import OperationRecord
from .store import ACTIVE, CANCELLED, PAUSED, Job, JobStore, iso, parse_dt, utcnow

log = logging.getLogger(__name__)

KIND_SCHEDULE_SEND = "automation.schedule_send"
KIND_REMINDER = "automation.reminder"
KIND_SNOOZE = "automation.snooze"
KIND_RULE = "automation.rule"
KIND_WEBHOOK = "automation.webhook"
KIND_EDIT = "automation.edit"
KIND_CANCEL = "automation.cancel"

# job type -> registration kind
REGISTER_KINDS: dict[str, str] = {
    "scheduled_send": KIND_SCHEDULE_SEND,
    "reminder": KIND_REMINDER,
    "snooze": KIND_SNOOZE,
    "rule": KIND_RULE,
}

LOCAL_LABELS: dict[str, str] = {
    "scheduled_send": ("local schedule: requires this machine and the mcp-proton service to be "
                       "running at the scheduled time; not Proton's native scheduled send"),
    "reminder": "local reminder: emitted by this service as an event; not a Proton feature",
    "snooze": ("local automation, not Proton's native snooze: the message is filed in the "
               "chosen folder and restored by this machine and service at the chosen time"),
    "rule": ("local rule run by this service; it does not create or edit Proton's "
             "server-side filters"),
}


# ------------------------------------------------------------------ job execution types


@dataclass
class JobContext:
    app: MailApp
    store: JobStore
    job: Job
    caller: CallerContext
    scheduled_for: datetime
    now: datetime
    idempotency_key: str  # stable per (job, scheduled time): a re-run never double-acts
    late: bool = False


@dataclass
class RunResult:
    """What a handler reports.

    ``status``: succeeded | partial | pending | blocked | failed | skipped.
    ``next_run_at`` / ``spec`` let multi-phase jobs (snooze) continue; ``finish`` forces
    the job to end (``True``) or continue (``False``) regardless of its type.
    """

    status: str
    message: str = ""
    operation_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    next_run_at: datetime | None = None
    spec: dict[str, Any] | None = None
    finish: bool | None = None


Handler = Callable[[JobContext], RunResult]
HANDLERS: dict[str, Handler] = {}


def handler(job_type: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[job_type] = fn
        return fn

    return deco


def job_caller(client_id: str) -> CallerContext:
    """The creator's identity when a job fires. ``client_id`` is what policy keys on."""
    return CallerContext(client_id=client_id, transport=Transport.JOB)


def run_status_of(outcome: OperationOutcome) -> str:
    return {
        OperationStatus.SUCCEEDED: "succeeded",
        OperationStatus.PARTIALLY_SUCCEEDED: "partial",
        OperationStatus.PENDING: "pending",
        OperationStatus.APPROVED: "pending",
        OperationStatus.DELIVERY_UNKNOWN: "failed",
    }.get(outcome.status, "failed")


def error_result(exc: MailError) -> RunResult:
    """A kernel refusal at fire time (revoked client, paused account, denied family...)."""
    blocked = exc.code in (ErrorCode.POLICY_DENIED, ErrorCode.CLIENT_REVOKED,
                           ErrorCode.ACCOUNT_PAUSED, ErrorCode.CONSTRAINT_VIOLATION,
                           ErrorCode.LIMIT_EXCEEDED, ErrorCode.NOT_ONBOARDED,
                           ErrorCode.PATH_REJECTED)
    return RunResult("blocked" if blocked else "failed", exc.message,
                     data={"error": exc.to_dict()})


# ------------------------------------------------------------------ identity / scoping


def check_not_revoked(app: MailApp, caller: CallerContext) -> None:
    if caller.is_owner:
        return
    cfg = app.policy.client(caller.client_id)
    if cfg is not None and cfg.revoked:
        raise MailError(ErrorCode.CLIENT_REVOKED, "this client has been revoked")


def visible(caller: CallerContext, job: Job) -> bool:
    return caller.is_owner or job.client_id == caller.client_id


def get_visible(app: MailApp, caller: CallerContext, job_id: str) -> Job:
    check_not_revoked(app, caller)
    job = JobStore(app.db).get(job_id)
    if job is None or not visible(caller, job):
        raise not_found("job not found", job_id=job_id)
    return job


# ------------------------------------------------------------------ registration


def register(app: MailApp, caller: CallerContext, *, job_type: str, account: str,
             spec: dict[str, Any], summary: str, next_run_at: datetime | None,
             timezone: str | None = None, missed_run_policy: str = "run_once",
             status: str = ACTIVE, mailboxes: list[str] | None = None,
             targets: list[MessageHandle] | None = None, recipients: list[str] | None = None,
             paths: list[str] | None = None, idempotency_key: str | None = None,
             extra: dict[str, Any] | None = None) -> OperationOutcome:
    """Submit a registration (family AUTOMATION). The executor creates the job."""
    app.config.account(account)  # validates the account exists
    kind = REGISTER_KINDS.get(job_type)
    if kind is None:
        raise invalid(f"unknown job type {job_type!r}")
    spec = {**spec, "label": LOCAL_LABELS[job_type]}
    req = OperationRequest(
        kind=kind, family=OperationFamily.AUTOMATION, account=account,
        mailboxes=list(dict.fromkeys(mailboxes or [])), targets=list(targets or []),
        recipients=list(recipients or []), paths=list(paths or []),
        batch_size=max(1, len(targets or [])),
        payload={"job_type": job_type, "spec": spec, "next_run_at": iso(next_run_at),
                 "timezone": timezone, "missed_run_policy": missed_run_policy,
                 "status": status, **(extra or {})},
        summary=summary, idempotency_key=idempotency_key,
    )
    return app.run(caller, req)


# job type -> function(app, job) run once when a job is created (e.g. set a trigger cursor)
ON_CREATE: dict[str, Callable[[MailApp, Job], None]] = {}


def _register_executor(app: MailApp, rec: OperationRecord) -> ExecResult:
    p = rec.request.payload
    job = JobStore(app.db).create(
        job_type=p["job_type"], client_id=rec.client_id, account=rec.account, spec=p["spec"],
        next_run_at=parse_dt(p["next_run_at"]), timezone=p.get("timezone"),
        missed_run_policy=p.get("missed_run_policy", "run_once"),
        job_id="job_" + rec.id.removeprefix("op_"), status=p.get("status", ACTIVE))
    hook = ON_CREATE.get(job.type)
    if hook is not None:
        hook(app, job)
    return ExecResult(OperationStatus.SUCCEEDED, {
        "job_id": job.id, "type": job.type, "next_run_at": iso(job.next_run_at),
        "note": job.spec.get("label")})


for _kind in REGISTER_KINDS.values():
    executor(_kind, OperationFamily.AUTOMATION)(_register_executor)


# ------------------------------------------------------------------ edit / cancel

# job type -> function(job, changes, now) -> dict of store.update kwargs
EDITORS: dict[str, Callable[[Job, dict[str, Any], datetime], dict[str, Any]]] = {}


Edit = Callable[..., dict[str, Any]]


def editor(job_type: str) -> Callable[[Edit], Edit]:
    def deco(fn: Edit) -> Edit:
        EDITORS[job_type] = fn
        return fn

    return deco


def edit_job(app: MailApp, caller: CallerContext, job_id: str, changes: dict[str, Any]
             ) -> OperationOutcome:
    """Edit timing / policy / enabled state of a job (family AUTOMATION)."""
    job = get_visible(app, caller, job_id)
    if job.status not in (ACTIVE, PAUSED):
        raise MailError(ErrorCode.CONFLICT, f"job is {job.status}; it can no longer be edited")
    if not changes:
        raise invalid("no changes given")
    fn = EDITORS.get(job.type)
    if fn is None:
        raise MailError(ErrorCode.UNSUPPORTED, f"{job.type} jobs cannot be edited; cancel it "
                        "and create a new one")
    fn(job, dict(changes), utcnow())  # validate now; the executor applies it again
    req = OperationRequest(
        kind=KIND_EDIT, family=OperationFamily.AUTOMATION, account=job.account,
        payload={"job_id": job.id, "changes": changes},
        summary=f"Edit {job.type.replace('_', ' ')} {job.id}")
    return app.run(caller, req)


@executor(KIND_EDIT, OperationFamily.AUTOMATION)
def _edit_executor(app: MailApp, rec: OperationRecord) -> ExecResult:
    p = rec.request.payload
    store = JobStore(app.db)
    job = store.get(p["job_id"])
    if job is None or job.status not in (ACTIVE, PAUSED):
        raise MailError(ErrorCode.CONFLICT, "job no longer exists or is finished")
    update = EDITORS[job.type](job, dict(p["changes"]), utcnow())
    store.update(job.id, **update)
    fresh = store.get(job.id)
    assert fresh is not None
    return ExecResult(OperationStatus.SUCCEEDED, {"job": fresh.public()})


def cancel_job(app: MailApp, caller: CallerContext, job_id: str) -> OperationOutcome:
    job = get_visible(app, caller, job_id)
    if job.status in (CANCELLED,):
        raise MailError(ErrorCode.CONFLICT, "job is already cancelled")
    req = OperationRequest(
        kind=KIND_CANCEL, family=OperationFamily.AUTOMATION, account=job.account,
        payload={"job_id": job.id},
        summary=f"Cancel {job.type.replace('_', ' ')} {job.id}")
    return app.run(caller, req)


@executor(KIND_CANCEL, OperationFamily.AUTOMATION)
def _cancel_executor(app: MailApp, rec: OperationRecord) -> ExecResult:
    if "webhook_id" in rec.request.payload:
        from .webhooks import delete_row  # lazy: webhooks imports this module

        return ExecResult(OperationStatus.SUCCEEDED,
                          delete_row(app, rec.request.payload["webhook_id"]))
    store = JobStore(app.db)
    job = store.get(rec.request.payload["job_id"])
    if job is None:
        raise not_found("job not found")
    note = None
    if job.type == "snooze" and job.spec.get("phase") == "restore":
        note = "cancelling a snooze does not move the messages back; they stay where they are"
    store.update(job.id, status=CANCELLED, next_run_at=None)
    return ExecResult(OperationStatus.SUCCEEDED, {"job_id": job.id, "status": CANCELLED,
                                                  **({"note": note} if note else {})})


# ------------------------------------------------------------------ listing


def list_jobs(app: MailApp, caller: CallerContext, *, job_type: str | None = None,
              status: str | None = None, account: str | None = None, limit: int = 100
              ) -> list[dict[str, Any]]:
    """Caller-scoped: a client sees its own jobs, the owner sees all."""
    check_not_revoked(app, caller)
    jobs = JobStore(app.db).find(client_id=None if caller.is_owner else caller.client_id,
                                 job_type=job_type, status=status, account=account, limit=limit)
    return [j.public() for j in jobs]


def get_job(app: MailApp, caller: CallerContext, job_id: str, runs: int = 10
            ) -> dict[str, Any]:
    job = get_visible(app, caller, job_id)
    out = job.public()
    out["runs"] = [r.public() for r in JobStore(app.db).runs(job.id, runs)]
    return out
