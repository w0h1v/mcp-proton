"""Local reminders and snooze.

Both are **local automation**, not Proton features: a reminder is an event
(``reminder_due``) written to this service's event journal; snooze files a
message into a folder now and moves it back at the chosen time, tracked by the
handles recorded when it was filed. Proton's native snooze exists only in its
web and mobile apps.

Snooze is all-or-nothing at registration: the job starts in phase ``file`` and
performs the move through ``MailApp.run`` as the creator (so an Ask on organizing
leaves nothing half-done: the pending move is cancelled and the job ends
``blocked``). It then waits in phase ``restore`` until ``until``.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..domain.errors import ErrorCode, MailError, invalid
from ..domain.models import MailboxRole, MessageHandle, OperationOutcome, OperationStatus
from ..domain.requests import CallerContext
from ..services import messages
from ..services.common import canonical_mailbox, parse_handles
from ..services.core import MailApp
from ..services.events import EventStore
from . import common
from .common import JobContext, RunResult, error_result
from .schedules import parse_when
from .store import ACTIVE, PAUSED, Job, iso, parse_dt, utcnow

MAX_NOTE = 500
REMINDER_EVENT = "reminder_due"


def emit_event(app: MailApp, account: str, mailbox: str | None, type: str,  # noqa: A002
               data: dict[str, Any]) -> int:
    """Append to the shared event journal. ``reminder_due`` is a local-automation event
    type, so this uses the journal's insert directly instead of ``EventStore.append``."""
    with app.db.tx() as c:
        return EventStore._insert(c, account, mailbox, type, data)  # noqa: SLF001


# ------------------------------------------------------------------ remind


def remind(app: MailApp, caller: CallerContext, handle: str, at: datetime | str,
           note: str = "", *, timezone: str | None = None, star: bool = False,
           now: datetime | None = None) -> OperationOutcome:
    """Emit a ``reminder_due`` event for ``handle`` at ``at`` (optionally starring it then)."""
    h = MessageHandle.parse(handle)
    app.config.account(h.account)
    app.authorize_read(caller, h.account, [h.mailbox], kind="reminders.create")
    when = parse_when(at, timezone)
    if when <= (now or utcnow()):
        raise invalid("`at` is in the past")
    if len(note) > MAX_NOTE:
        raise invalid(f"note is limited to {MAX_NOTE} characters")
    mailboxes = [h.mailbox]
    return common.register(
        app, caller, job_type="reminder", account=h.account,
        spec={"handle": h.token(), "note": note, "star": star, "at": iso(when)},
        summary=f"Local reminder at {iso(when)}" + (" (will star the message)" if star else ""),
        next_run_at=when, timezone=timezone, missed_run_policy="run_once",
        mailboxes=mailboxes, targets=[h])


def _already_emitted(app: MailApp, key: str) -> bool:
    rows = app.db.query("SELECT 1 FROM events WHERE type=? AND data_json LIKE ? LIMIT 1",
                        (REMINDER_EVENT, f'%"run_key":{json.dumps(key)}%'))
    return bool(rows)


@common.handler("reminder")
def fire_reminder(ctx: JobContext) -> RunResult:
    spec = ctx.job.spec
    h = MessageHandle.parse(spec["handle"])
    try:
        ctx.app.authorize_read(ctx.caller, h.account, [h.mailbox], kind="reminders.fire")
    except MailError as exc:
        return error_result(exc)
    exists = True
    try:
        exists = h.uid in ctx.app.store(h.account).fetch_flags(h.mailbox, h.uidvalidity, [h.uid])
    except MailError:
        exists = False
    if not _already_emitted(ctx.app, ctx.idempotency_key):
        emit_event(ctx.app, h.account, h.mailbox, REMINDER_EVENT, {
            "handle": h.token(), "uid": h.uid, "job_id": ctx.job.id,
            "note": spec.get("note") or None, "message_exists": exists,
            "run_key": ctx.idempotency_key})
    data: dict[str, Any] = {"message_exists": exists}
    status = "succeeded"
    if spec.get("star") and exists:
        try:
            out = messages.set_flags(ctx.app, ctx.caller, [h.token()], add=["star"])
        except MailError as exc:
            return RunResult("partial", f"reminder emitted; starring failed: {exc.message}",
                             data={**data, "error": exc.to_dict()})
        data["star"] = {"operation_id": out.operation_id, "status": out.status.value}
        if out.status is not OperationStatus.SUCCEEDED:
            status = "partial"
        return RunResult(status, "reminder emitted", out.operation_id, data)
    return RunResult(status, "reminder emitted", data=data)


@common.editor("reminder")
def edit_reminder(job: Job, changes: dict[str, Any], now: datetime) -> dict[str, Any]:
    unknown = sorted(set(changes) - {"at", "timezone", "note", "star", "enabled"})
    if unknown:
        raise invalid(f"cannot edit {', '.join(unknown)}")
    spec = dict(job.spec)
    update: dict[str, Any] = {}
    if "at" in changes:
        when = parse_when(changes["at"], changes.get("timezone", job.timezone))
        if when <= now:
            raise invalid("`at` is in the past")
        spec["at"] = iso(when)
        update["next_run_at"] = when
    if "note" in changes:
        if len(changes["note"]) > MAX_NOTE:
            raise invalid(f"note is limited to {MAX_NOTE} characters")
        spec["note"] = changes["note"]
    if "star" in changes:
        spec["star"] = bool(changes["star"])
    if "enabled" in changes:
        update["status"] = ACTIVE if changes["enabled"] else PAUSED
    return {**update, "spec": spec}


# ------------------------------------------------------------------ snooze


def _check_folder(app: MailApp, account: str, folder: str) -> None:
    store = app.store(account)
    if folder not in {m.name for m in store.list_mailboxes()}:
        raise invalid("snooze folder does not exist")
    if store.role_of(folder) not in (MailboxRole.ARCHIVE, MailboxRole.FOLDER):
        raise invalid("snooze folder must be Archive or a folder under Folders/")


def snooze(app: MailApp, caller: CallerContext, handles: list[str], until: datetime | str,
           folder: str, *, timezone: str | None = None, now: datetime | None = None
           ) -> OperationOutcome:
    """File ``handles`` into ``folder`` now and move them back to where they were at ``until``.

    Registration is AUTOMATION; the filing move is ORGANIZE and the restore move is
    ORGANIZE at ``until`` (all through ``MailApp.run``). Returns the registration
    outcome; when it executed, ``result`` also carries ``filing`` (the filing step's run).
    """
    parsed = parse_handles(handles)
    account = parsed[0].account
    app.config.account(account)
    when = parse_when(until, timezone)
    now = now or utcnow()
    if when <= now:
        raise invalid("`until` is in the past")
    folder = canonical_mailbox(app, account, folder)
    _check_folder(app, account, folder)
    store = app.store(account)
    for h in parsed:
        if store.role_of(h.mailbox) in (MailboxRole.LABEL, MailboxRole.ALL_MAIL,
                                        MailboxRole.STARRED, MailboxRole.SCHEDULED,
                                        MailboxRole.CONTAINER, MailboxRole.OTHER):
            raise invalid("only messages in a folder, Inbox, Archive, Spam or Trash can be "
                          "snoozed")
    app.authorize_read(caller, account, sorted({h.mailbox for h in parsed}),
                       kind="snooze.create")
    out = common.register(
        app, caller, job_type="snooze", account=account,
        spec={"phase": "file", "folder": folder, "until": iso(when),
              "items": [{"handle": h.token(), "origin": h.mailbox} for h in parsed]},
        summary=f"Local snooze of {len(parsed)} message(s) into {folder} until {iso(when)}",
        next_run_at=now, timezone=timezone,
        mailboxes=[*{h.mailbox for h in parsed}, folder], targets=parsed)
    job_id = (out.result or {}).get("job_id")
    if out.status is OperationStatus.SUCCEEDED and job_id:
        from .scheduler import fire_job  # lazy: scheduler imports this module's handlers

        out.result = {**(out.result or {}), "filing": fire_job(app, job_id, now=now)}
    return out


@common.handler("snooze")
def fire_snooze(ctx: JobContext) -> RunResult:
    spec = dict(ctx.job.spec)
    items: list[dict[str, str]] = spec["items"]
    if spec["phase"] == "file":
        return _file(ctx, spec, items)
    return _restore(ctx, items)


def _file(ctx: JobContext, spec: dict[str, Any], items: list[dict[str, str]]) -> RunResult:
    origin = {i["handle"]: i["origin"] for i in items}
    try:
        out = messages.move(ctx.app, ctx.caller, list(origin), spec["folder"])
    except MailError as exc:
        return error_result(exc)
    if out.status in (OperationStatus.PENDING, OperationStatus.APPROVED):
        ctx.app.journal.cancel(out.operation_id)  # never leave a half-applied snooze pending
        return RunResult("blocked", "moving the messages needs approval, so the snooze was "
                         "not applied", out.operation_id, finish=True)
    moved = [{"handle": i.new_handle, "origin": origin[i.target]}
             for i in out.items or [] if i.status == "succeeded" and i.new_handle
             and i.target in origin]
    lost = sum(1 for i in out.items or [] if i.status == "succeeded" and not i.new_handle)
    if not moved:
        return RunResult("failed", "no message could be filed; snooze not applied",
                         out.operation_id, finish=True)
    partial = len(moved) < len(origin)
    data = {"filed": len(moved), "requested": len(origin)}
    if lost:
        data["unrestorable"] = lost
    return RunResult("partial" if partial else "succeeded", f"filed {len(moved)} message(s)",
                     out.operation_id, data, next_run_at=parse_dt(spec["until"]),
                     spec={**spec, "phase": "restore", "items": moved}, finish=False)


def _restore(ctx: JobContext, items: list[dict[str, str]]) -> RunResult:
    by_origin: dict[str, list[str]] = {}
    for i in items:
        by_origin.setdefault(i["origin"], []).append(i["handle"])
    failures: list[dict[str, str | None]] = []
    restored, pending = 0, 0
    last_op: str | None = None
    for mailbox, tokens in by_origin.items():
        try:
            out = messages.move(ctx.app, ctx.caller, tokens, mailbox)
        except MailError as exc:
            if exc.code in (ErrorCode.POLICY_DENIED, ErrorCode.CLIENT_REVOKED,
                            ErrorCode.ACCOUNT_PAUSED):
                return error_result(exc)
            failures += [{"handle": t, "detail": exc.code.value} for t in tokens]
            continue
        last_op = out.operation_id
        if out.status in (OperationStatus.PENDING, OperationStatus.APPROVED):
            pending += len(tokens)
            continue
        for it in out.items or []:
            if it.status == "succeeded":
                restored += 1
            elif it.status == "failed":
                failures.append({"handle": it.target, "detail": it.detail})
    data: dict[str, Any] = {"restored": restored, "failed": len(failures)}
    if failures:
        data["failures"] = failures[:50]
    if pending:
        data["pending"] = pending
        return RunResult("pending", "restoring needs approval", last_op, data)
    status = "succeeded" if not failures else ("partial" if restored else "failed")
    return RunResult(status, f"restored {restored} message(s)", last_op, data)


@common.editor("snooze")
def edit_snooze(job: Job, changes: dict[str, Any], now: datetime) -> dict[str, Any]:
    unknown = sorted(set(changes) - {"until", "timezone", "enabled"})
    if unknown:
        raise invalid(f"cannot edit {', '.join(unknown)}")
    spec = dict(job.spec)
    update: dict[str, Any] = {}
    if "until" in changes:
        when = parse_when(changes["until"], changes.get("timezone", job.timezone))
        if when <= now:
            raise invalid("`until` is in the past")
        spec["until"] = iso(when)
        if spec["phase"] == "restore":
            update["next_run_at"] = when
    if "enabled" in changes:
        update["status"] = ACTIVE if changes["enabled"] else PAUSED
    return {**update, "spec": spec}

