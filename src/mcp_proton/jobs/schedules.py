"""Local send schedules and the time arithmetic shared by every job type.

A schedule is **local automation**: it only fires while this machine and the
mcp-proton service are running. It is not Proton's native scheduled send.

Content is immutable once registered:

* ``content`` mode stores the full outgoing message plus an attachment manifest
  (sha256 per file). At fire time the attachments are re-read and compared; a
  changed or missing file fails the run.
* ``draft`` mode stores the draft handle and the sha256 of the draft as it was
  when scheduled. If the draft is edited (handles change on update), moved or
  deleted, the run fails with a clear reason and nothing is sent.

Firing goes through ``sending.send`` / ``drafts.send_draft`` with an idempotency
key derived from the job id and the *scheduled* time, so a crash between "SMTP
accepted" and "run recorded" never sends twice.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from ..bridge import mime
from ..domain.errors import ErrorCode, MailError, invalid
from ..domain.models import (
    ArtifactAttachment,
    LocalAttachment,
    MessageHandle,
    Model,
    OperationOutcome,
    OutgoingMessage,
)
from ..domain.requests import CallerContext
from ..services import attachments as att
from ..services import drafts, sending
from ..services.core import MailApp
from . import common
from .common import JobContext, RunResult, error_result, run_status_of
from .store import ACTIVE, MISSED_POLICIES, PAUSED, Job, iso, utcnow

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
MAX_LOOKAHEAD_DAYS = 15


# ------------------------------------------------------------------ time arithmetic


def zone_of(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise invalid(f"unknown timezone {name!r}; use an IANA name such as Europe/Berlin") from exc


def local_to_utc(day: Any, hour: int, minute: int, zone: ZoneInfo) -> datetime:
    """Wall-clock ``day hour:minute`` in ``zone`` as UTC, DST-safe.

    A time skipped by a spring-forward gap runs at the first valid instant after
    the gap (02:30 -> 03:30); a time repeated by a fall-back runs once, at its
    first occurrence.
    """
    naive = datetime(day.year, day.month, day.day, hour, minute)
    return naive.replace(tzinfo=zone, fold=0).astimezone(UTC)


class Recurrence(Model):
    """Daily or weekly at a local wall-clock time."""

    freq: Literal["daily", "weekly"]
    time: str = "09:00"
    weekdays: list[int] = Field(default_factory=list)  # Monday=0 .. Sunday=6
    timezone: str

    @field_validator("time")
    @classmethod
    def _time(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError("time must be HH:MM (24 hour)")
        return v

    @field_validator("weekdays")
    @classmethod
    def _weekdays(cls, v: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("weekdays are 0 (Monday) to 6 (Sunday)")
        return sorted(set(v))

    @model_validator(mode="after")
    def _check(self) -> Recurrence:
        try:
            zone_of(self.timezone)
        except MailError as exc:
            raise ValueError(exc.message) from exc
        if self.freq == "weekly" and not self.weekdays:
            raise ValueError("a weekly recurrence needs weekdays")
        return self

    def next_after(self, after: datetime) -> datetime:
        """First occurrence strictly after ``after`` (UTC)."""
        if after.tzinfo is None:
            raise ValueError("naive datetime")
        zone = zone_of(self.timezone)
        hh, mm = (int(x) for x in self.time.split(":"))
        first = after.astimezone(zone).date()
        for i in range(MAX_LOOKAHEAD_DAYS):
            day = first + timedelta(days=i)
            if self.weekdays and day.weekday() not in self.weekdays:
                continue
            cand = local_to_utc(day, hh, mm, zone)
            if cand > after:
                return cand
        raise MailError(ErrorCode.INTERNAL, "could not compute the next occurrence")

    def occurrences(self, after: datetime, upto: datetime, limit: int = 10_000
                    ) -> list[datetime]:
        """Occurrences in ``(after, upto]``."""
        out: list[datetime] = []
        cur = after
        while len(out) < limit:
            cur = self.next_after(cur)
            if cur > upto:
                break
            out.append(cur)
        return out


def parse_recurrence(raw: dict[str, Any] | Recurrence, default_tz: str | None = None
                     ) -> Recurrence:
    if isinstance(raw, Recurrence):
        return raw
    data = dict(raw)
    if "timezone" not in data and default_tz:
        data["timezone"] = default_tz
    try:
        return Recurrence.model_validate(data)
    except ValueError as exc:
        raise invalid(f"invalid recurrence: {exc}") from exc


def parse_when(at: datetime | str, timezone: str | None = None) -> datetime:
    """A one-shot timestamp as UTC. Naive input is read in ``timezone`` (required)."""
    if isinstance(at, str):
        text = at.strip()
        try:
            dt = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError as exc:
            raise invalid("`at` must be an ISO-8601 timestamp, e.g. 2026-10-12T09:00:00+02:00"
                          ) from exc
    else:
        dt = at
    if dt.tzinfo is None:
        if not timezone:
            raise invalid("a timestamp without an offset needs a `timezone` (IANA name)")
        dt = dt.replace(tzinfo=zone_of(timezone), fold=0)
    return dt.astimezone(UTC)


def check_missed_policy(policy: str) -> str:
    if policy not in MISSED_POLICIES:
        raise invalid(f"missed_run_policy must be one of {sorted(MISSED_POLICIES)}")
    return policy


def recurrence_of(job: Job) -> Recurrence | None:
    raw = job.spec.get("recurrence")
    return Recurrence.model_validate(raw) if raw else None


# ------------------------------------------------------------------ registration


def _canonical_sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def _attachment_items(manifest: list[dict[str, Any]]
                      ) -> list[LocalAttachment | ArtifactAttachment]:
    items: list[LocalAttachment | ArtifactAttachment] = []
    for e in manifest:
        if e["source"] == "local":
            items.append(LocalAttachment(path=e["path"], filename=e["filename"],
                                         content_type=e["content_type"],
                                         inline_cid=e.get("inline_cid")))
        else:
            items.append(ArtifactAttachment(artifact_id=e["artifact_id"],
                                            filename=e["filename"],
                                            inline_cid=e.get("inline_cid")))
    return items


def schedule_send(app: MailApp, caller: CallerContext, *,
                  message: OutgoingMessage | None = None, draft: str | None = None,
                  at: datetime | str | None = None, timezone: str | None = None,
                  recurrence: dict[str, Any] | None = None,
                  missed_run_policy: str = "run_once", now: datetime | None = None,
                  idempotency_key: str | None = None) -> OperationOutcome:
    """Register a local send schedule (policy family AUTOMATION).

    Pass exactly one of ``message`` (immutable content) or ``draft`` (a draft handle,
    bound to the draft's sha256), and exactly one of ``at`` or ``recurrence``.
    """
    if (message is None) == (draft is None):
        raise invalid("pass exactly one of message or draft")
    if (at is None) == (recurrence is None):
        raise invalid("pass exactly one of at or recurrence")
    check_missed_policy(missed_run_policy)
    now = now or utcnow()
    rec: Recurrence | None = None
    if recurrence is not None:
        if draft is not None:
            raise invalid("a recurring schedule needs immutable message content, not a draft")
        rec = parse_recurrence(recurrence, timezone)
        first = rec.next_after(now)
        timezone = rec.timezone
    else:
        assert at is not None
        first = parse_when(at, timezone)
        if first <= now:
            raise invalid("`at` is in the past")

    spec: dict[str, Any] = {"recurrence": rec.model_dump() if rec else None,
                            "at": None if rec else iso(first)}
    mailboxes: list[str] = []
    recipients: list[str] = []
    paths: list[str] = []
    extra: dict[str, Any] = {}
    if message is not None:
        account = message.account
        cfg = app.config.account(account)
        from_addr = sending.sender_identity(cfg, message.from_)
        recipients = sending.dedupe_addresses(message.all_recipients())
        if not recipients:
            raise invalid("at least one recipient is required")
        mime.build_message(message, from_addr=from_addr, message_id="<validate@invalid>")
        manifest = att.build_manifest(app, caller, list(message.attachments), account)
        send = message.model_dump(by_alias=True, mode="json", exclude={"attachments"})
        send["attachment_manifest"] = manifest
        spec.update(mode="content", send=send, content_sha256=_canonical_sha(send))
        paths = att.manifest_paths(manifest)
        if paths:
            extra = {"_path_family": "attachment_ingest",
                     "_also_families": ["attachment_ingest"]}
        subject = sending.clip_text(message.subject)
    else:
        assert draft is not None
        h = MessageHandle.parse(draft)
        account = h.account
        info = drafts.get_draft(app, caller, draft)  # authorizes the read; revision = sha256
        spec.update(mode="draft", draft={"handle": h.token(), "mailbox": h.mailbox,
                                         "sha256": info["revision"]})
        mailboxes = [h.mailbox]
        subject = sending.clip_text(info["draft"].get("subject") or "")
    where = f"every {rec.freq} {rec.time} {rec.timezone}" if rec else f"at {iso(first)}"
    return common.register(
        app, caller, job_type="scheduled_send", account=account, spec=spec,
        summary=f"Local schedule: send {where}: {subject}", next_run_at=first,
        timezone=timezone, missed_run_policy=missed_run_policy, mailboxes=mailboxes,
        recipients=recipients, paths=paths, extra=extra, idempotency_key=idempotency_key)


# ------------------------------------------------------------------ firing


def _verify_attachments(ctx: JobContext, send: dict[str, Any]
                        ) -> list[LocalAttachment | ArtifactAttachment]:
    stored = send.get("attachment_manifest", [])
    items = _attachment_items(stored)
    try:
        current = att.build_manifest(ctx.app, ctx.caller, items, ctx.job.account)
    except MailError as exc:
        if exc.code in (ErrorCode.NOT_FOUND, ErrorCode.PATH_REJECTED):
            raise MailError(ErrorCode.CONFLICT,
                            "an attachment is missing or no longer readable since scheduling"
                            ) from exc
        raise
    for old, new in zip(stored, current, strict=True):
        if old["sha256"] != new["sha256"] or old["size"] != new["size"]:
            raise MailError(ErrorCode.CONFLICT,
                            f"attachment {old['filename']!r} changed since scheduling")
    return items


def _outcome_result(out: OperationOutcome) -> RunResult:
    data: dict[str, Any] = {}
    if out.result and out.result.get("message_id"):
        data["message_id"] = out.result["message_id"]
    if out.error:
        data["error"] = out.error
    return RunResult(run_status_of(out), out.summary, out.operation_id, data)


def _check_draft(ctx: JobContext, d: dict[str, Any]) -> RunResult | None:
    """None when the draft is still exactly what was scheduled."""
    h = MessageHandle.parse(d["handle"])
    ctx.app.authorize_read(ctx.caller, h.account, [h.mailbox], kind="drafts.get")
    try:
        raw = ctx.app.store(h.account).fetch_message(h.mailbox, h.uidvalidity, h.uid).raw
    except MailError as exc:
        if exc.code in (ErrorCode.NOT_FOUND, ErrorCode.STALE_HANDLE):
            return RunResult("failed", "the scheduled draft was edited, moved or deleted since "
                             "scheduling (draft handles change when a draft is updated); "
                             "nothing was sent",
                             data={"error": exc.to_dict(), "reason": "draft_missing"})
        raise
    if hashlib.sha256(raw).hexdigest() != d["sha256"]:
        return RunResult("failed", "the scheduled draft changed since scheduling; nothing was "
                         "sent. Schedule the new version instead", data={"reason": "draft_changed"})
    return None


@common.handler("scheduled_send")
def fire_send(ctx: JobContext) -> RunResult:
    key = ctx.idempotency_key
    prior = ctx.app.journal.find_idempotent(ctx.caller.client_id, key)
    if prior is not None:  # recovery after a crash: report what already happened
        return _outcome_result(ctx.app.status(ctx.caller, prior.id))
    spec = ctx.job.spec
    try:
        if spec["mode"] == "draft":
            d = spec["draft"]
            bad = _check_draft(ctx, d)
            if bad is not None:
                return bad
            return _outcome_result(drafts.send_draft(ctx.app, ctx.caller, d["handle"],
                                                     idempotency_key=key))
        send = spec["send"]
        items = _verify_attachments(ctx, send)
        out = OutgoingMessage.model_validate(
            {**{k: v for k, v in send.items() if k != "attachment_manifest"},
             "attachments": items})
        return _outcome_result(sending.send(ctx.app, ctx.caller, out, idempotency_key=key))
    except MailError as exc:
        if exc.code is ErrorCode.CONFLICT:
            return RunResult("failed", exc.message, data={"error": exc.to_dict()})
        return error_result(exc)


# ------------------------------------------------------------------ editing


@common.editor("scheduled_send")
def edit_schedule(job: Job, changes: dict[str, Any], now: datetime) -> dict[str, Any]:
    allowed = {"at", "timezone", "recurrence", "missed_run_policy", "enabled"}
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise invalid(f"cannot edit {', '.join(unknown)}; message content is immutable "
                      "(cancel the schedule and create a new one)")
    spec = dict(job.spec)
    tz = changes.get("timezone", job.timezone)
    update: dict[str, Any] = {}
    next_run = job.next_run_at
    if "recurrence" in changes and changes["recurrence"] is not None:
        if spec.get("mode") == "draft":
            raise invalid("a draft schedule cannot recur")
        rec = parse_recurrence(changes["recurrence"], tz)
        spec.update(recurrence=rec.model_dump(), at=None)
        next_run, tz = rec.next_after(now), rec.timezone
    elif "at" in changes:
        when = parse_when(changes["at"], tz)
        if when <= now:
            raise invalid("`at` is in the past")
        spec.update(at=iso(when), recurrence=None)
        next_run = when
    elif "timezone" in changes and spec.get("recurrence"):
        rec = parse_recurrence({**spec["recurrence"], "timezone": changes["timezone"]})
        spec["recurrence"] = rec.model_dump()
        next_run, tz = rec.next_after(now), rec.timezone
    if "missed_run_policy" in changes:
        update["missed_run_policy"] = check_missed_policy(changes["missed_run_policy"])
    if "enabled" in changes:
        update["status"] = ACTIVE if changes["enabled"] else PAUSED
        if changes["enabled"] and job.status == PAUSED and spec.get("recurrence") \
                and "recurrence" not in changes:
            next_run = parse_recurrence(spec["recurrence"]).next_after(now)
    return {**update, "spec": spec, "timezone": tz, "next_run_at": next_run}

