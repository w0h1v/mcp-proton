"""Local rules: match messages, then file / label / flag them.

Rules are **local automation** run by this service. They do not create or edit
Proton's server-side filters. A rule can be previewed (read-only), run manually,
and optionally triggered by new mail (``message_added`` events after a stored
cursor). Every action goes through ``MailApp.run``, so policy applies exactly as
for any other organizing write, as the invoking client (manual run) or as the
rule's creator with ``Transport.JOB`` (trigger).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from ..domain.errors import ErrorCode, MailError, invalid, not_found
from ..domain.models import (
    MessageSummary,
    Model,
    OperationOutcome,
    OperationStatus,
    SearchQuery,
    canonical_inbox,
)
from ..domain.requests import CallerContext
from ..services import messages
from ..services.common import MAX_BATCH, canonical_mailbox
from ..services.core import MailApp
from . import common
from .common import job_caller
from .store import ACTIVE, PAUSED, Job, JobStore, utcnow

log = logging.getLogger(__name__)

PREVIEW_SAMPLE = 50
SCAN_LIMIT = 1000
TRIGGER_PAGE = 200
TRIGGER_PAGES_PER_TICK = 5


# ------------------------------------------------------------------ model


class RuleMatch(Model):
    sender_contains: str | None = None
    sender_domain: str | None = None
    subject_contains: str | None = None
    to_contains: str | None = None
    has_attachments: bool | None = None

    @model_validator(mode="after")
    def _any(self) -> RuleMatch:
        if all(v is None for v in self.model_dump().values()):
            raise ValueError("a rule needs at least one match condition")
        for k, v in self.model_dump().items():
            if isinstance(v, str) and not v.strip():
                raise ValueError(f"{k} must not be empty")
        return self


class RuleAction(Model):
    type: Literal["move", "label", "flags"]
    target: str | None = None  # folder (move) or label (label)
    add: list[str] = Field(default_factory=list)  # flags: read, star, ...
    remove: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> RuleAction:
        if self.type in ("move", "label") and not self.target:
            raise ValueError(f"{self.type} needs a target")
        if self.type == "flags":
            if not (self.add or self.remove):
                raise ValueError("flags needs add or remove")
            messages.normalize_flags(self.add, self.remove)
        return self


class Rule(Model):
    name: str = Field(min_length=1, max_length=80)
    mailbox: str = "INBOX"  # scope for preview and manual runs
    match: RuleMatch
    actions: list[RuleAction] = Field(min_length=1, max_length=5)
    trigger: bool = False  # also run on new mail (message_added events)
    trigger_mailboxes: list[str] = Field(default_factory=lambda: ["INBOX"])

    @field_validator("mailbox")
    @classmethod
    def _canonical_mailbox(cls, v: str) -> str:
        return canonical_inbox(v)

    @field_validator("trigger_mailboxes")
    @classmethod
    def _canonical_triggers(cls, v: list[str]) -> list[str]:
        return [canonical_inbox(m) for m in v]

    @field_validator("actions")
    @classmethod
    def _one_move(cls, v: list[RuleAction]) -> list[RuleAction]:
        if sum(a.type == "move" for a in v) > 1:
            raise ValueError("a rule can move to at most one folder")
        return v


def parse_rule(raw: dict[str, Any] | Rule) -> Rule:
    if isinstance(raw, Rule):
        return raw
    try:
        return Rule.model_validate(raw)
    except ValueError as exc:
        raise invalid(f"invalid rule: {exc}") from exc


# ------------------------------------------------------------------ matching


def _addr_text(addrs: list[Any]) -> list[str]:
    out: list[str] = []
    for a in addrs:
        out.append(a.email.lower())
        if a.name:
            out.append(a.name.lower())
    return out


def matches(m: RuleMatch, s: MessageSummary) -> bool:
    if m.sender_contains and not any(m.sender_contains.lower() in t for t in _addr_text(s.from_)):
        return False
    if m.sender_domain:
        dom = m.sender_domain.lower().lstrip("@")
        if not any(a.email.lower().rsplit("@", 1)[-1] == dom for a in s.from_):
            return False
    if m.subject_contains and m.subject_contains.lower() not in (s.subject or "").lower():
        return False
    if m.to_contains and not any(m.to_contains.lower() in t for t in _addr_text(s.to)):
        return False
    return not (m.has_attachments is not None and s.has_attachments is not m.has_attachments)


def _prefilter(m: RuleMatch) -> SearchQuery | None:
    """Server-side narrowing; the exact match is re-checked locally."""
    q: dict[str, Any] = {}
    if m.sender_contains:
        q["from"] = m.sender_contains
    elif m.sender_domain:
        q["from"] = "@" + m.sender_domain.lstrip("@")
    if m.subject_contains:
        q["subject"] = m.subject_contains
    if m.to_contains:
        q["to"] = m.to_contains
    return SearchQuery.model_validate(q) if q else None


def find_matches(app: MailApp, caller: CallerContext, account: str, mailbox: str, m: RuleMatch,
                 limit: int = SCAN_LIMIT) -> tuple[list[MessageSummary], int, bool]:
    """(matching summaries newest first, messages scanned, truncated)."""
    mailbox = canonical_mailbox(app, account, mailbox)
    app.authorize_read(caller, account, [mailbox], kind="rules.preview")
    store = app.store(account)
    q = _prefilter(m)
    if q is not None:
        uv, uids, _notes = store.search(mailbox, q)
    else:
        uv, uids = store.list_uids(mailbox)
    ordered = sorted(uids, reverse=True)
    limit = max(1, min(limit, SCAN_LIMIT))
    truncated = len(ordered) > limit
    out: list[MessageSummary] = []
    scanned = ordered[:limit]
    for i in range(0, len(scanned), 100):
        out += [s for s in store.fetch_summaries(mailbox, uv, scanned[i:i + 100])
                if matches(m, s)]
    out.sort(key=lambda s: s.uid, reverse=True)
    return out, len(scanned), truncated


# ------------------------------------------------------------------ validation


def _validate_targets(app: MailApp, account: str, rule: Rule) -> list[str]:
    names = messages.mailbox_names(app, account)
    used: list[str] = [rule.mailbox]
    if rule.mailbox not in names:
        raise not_found("rule mailbox does not exist")
    for a in rule.actions:
        if a.type == "move":
            assert a.target
            if a.target not in names:
                raise not_found("destination folder does not exist")
            used.append(a.target)
        elif a.type == "label":
            assert a.target
            full = a.target if a.target.startswith("Labels/") else f"Labels/{a.target}"
            if full not in names:
                raise not_found("label does not exist")
            used.append(full)
    return used


# ------------------------------------------------------------------ preview


def preview(app: MailApp, caller: CallerContext, account: str, rule: Rule | dict[str, Any],
            mailbox: str | None = None, limit: int = 200) -> dict[str, Any]:
    """Read-only: which messages match, the planned operations and their policy action."""
    r = parse_rule(rule)
    app.config.account(account)
    box = mailbox or r.mailbox
    found, scanned, truncated = find_matches(app, caller, account, box, r.match, limit)
    tokens = [s.handle for s in found[:MAX_BATCH]]
    planned: list[dict[str, Any]] = []
    for a in _ordered(r.actions):
        entry: dict[str, Any] = {"action": a.type, "target": a.target,
                                 "add": a.add, "remove": a.remove}
        if tokens:
            try:
                op, dest = _op_args(a)
                pv = messages.bulk_preview(app, caller, account, op, handles=tokens, dest=dest,
                                           add=a.add or None, remove=a.remove or None)
                entry["policy"] = pv.get("policy")
                entry["effects"] = [b.get("plan") or b.get("error") or b.get("note")
                                    for b in pv.get("by_mailbox", [])]
            except MailError as exc:
                entry["error"] = exc.to_dict()
        planned.append(entry)
    return {
        "rule": r.name, "account": account, "mailbox": box, "scanned": scanned,
        "matched": len(found), "truncated": truncated,
        "messages": [{"handle": t, "from": [a.email for a in s.from_][:3],
                      "subject": (s.subject or "")[:120]}
                     for t, s in zip(tokens, found, strict=False)][:PREVIEW_SAMPLE],
        "planned_operations": planned,
        "note": "preview only; nothing was changed. Local rule: it does not edit Proton's "
                "server-side filters.",
    }


def _ordered(actions: list[RuleAction]) -> list[RuleAction]:
    """Flags first, labels next, the (handle-changing) move last."""
    rank = {"flags": 0, "label": 1, "move": 2}
    return sorted(actions, key=lambda a: rank[a.type])


def _op_args(a: RuleAction) -> tuple[str, str | None]:
    if a.type == "move":
        return "move", a.target
    if a.type == "label":
        return "label_apply", a.target
    return "flags", None


# ------------------------------------------------------------------ execution


def apply_actions(app: MailApp, caller: CallerContext, handles: list[str],
                  actions: list[RuleAction]) -> list[dict[str, Any]]:
    """Run each action over ``handles`` through the kernel. Never raises for one action."""
    results: list[dict[str, Any]] = []
    for a in _ordered(actions):
        entry: dict[str, Any] = {"action": a.type, "target": a.target}
        try:
            out: OperationOutcome
            if a.type == "move":
                assert a.target
                out = messages.move(app, caller, handles, a.target)
            elif a.type == "label":
                assert a.target
                out = messages.label_apply(app, caller, handles, a.target)
            else:
                out = messages.set_flags(app, caller, handles, add=a.add, remove=a.remove)
            entry.update(operation_id=out.operation_id, status=_status(out),
                         counts=(out.result or {}).get("counts"))
        except MailError as exc:
            blocked = exc.code in (ErrorCode.POLICY_DENIED, ErrorCode.CLIENT_REVOKED,
                                   ErrorCode.ACCOUNT_PAUSED, ErrorCode.CONSTRAINT_VIOLATION)
            entry.update(status="blocked" if blocked else "failed", error=exc.to_dict())
        results.append(entry)
    return results


def _status(out: OperationOutcome) -> str:
    return {OperationStatus.SUCCEEDED: "succeeded",
            OperationStatus.PARTIALLY_SUCCEEDED: "partial",
            OperationStatus.PENDING: "pending"}.get(out.status, "failed")


def _overall(results: list[dict[str, Any]]) -> str:
    states = {r["status"] for r in results}
    if states == {"succeeded"}:
        return "succeeded"
    if "pending" in states and states <= {"pending", "succeeded"}:
        return "pending"
    if "blocked" in states and states == {"blocked"}:
        return "blocked"
    if "succeeded" in states or "partial" in states:
        return "partial"
    return "failed"


def get_rule(app: MailApp, caller: CallerContext, rule_id: str) -> Job:
    job = common.get_visible(app, caller, rule_id)
    if job.type != "rule":
        raise not_found("rule not found", job_id=rule_id)
    return job


def run(app: MailApp, caller: CallerContext, rule_id: str, mailbox: str | None = None,
        limit: int = 200, now: datetime | None = None) -> dict[str, Any]:
    """Manually execute a registered rule over a mailbox, as ``caller``."""
    job = get_rule(app, caller, rule_id)
    r = parse_rule(job.spec["rule"])
    box = mailbox or r.mailbox
    found, scanned, truncated = find_matches(app, caller, job.account, box, r.match, limit)
    tokens = [s.handle for s in found]
    results = apply_actions(app, caller, tokens, r.actions) if tokens else []
    status = _overall(results) if results else "succeeded"
    run_id = JobStore(app.db).record_run(
        job.id, status=status, now=now or utcnow(),
        operation_id=next((x.get("operation_id") for x in results if x.get("operation_id")), None),
        detail={"manual": True, "invoked_by": caller.client_id, "mailbox": box,
                "matched": len(tokens), "results": results})
    return {"rule_id": job.id, "run_id": run_id, "status": status, "mailbox": box,
            "scanned": scanned, "matched": len(tokens), "truncated": truncated,
            "results": results}


# ------------------------------------------------------------------ registration


def create(app: MailApp, caller: CallerContext, account: str, rule: Rule | dict[str, Any]
           ) -> OperationOutcome:
    """Register a rule (policy family AUTOMATION)."""
    r = parse_rule(rule)
    app.config.account(account)
    used = _validate_targets(app, account, r)
    trig = " (runs on new mail)" if r.trigger else ""
    return common.register(
        app, caller, job_type="rule", account=account,
        spec={"rule": r.model_dump(), "trigger": r.trigger}, next_run_at=None,
        summary=f"Local rule {r.name!r}{trig}", mailboxes=used)


def _on_create(app: MailApp, job: Job) -> None:
    store = JobStore(app.db)
    if store.cursor(f"rule:{job.id}") is None:
        store.set_cursor(f"rule:{job.id}", store.max_event_seq())


common.ON_CREATE["rule"] = _on_create


@common.editor("rule")
def edit_rule(job: Job, changes: dict[str, Any], now: datetime) -> dict[str, Any]:
    unknown = sorted(set(changes) - {"name", "mailbox", "match", "actions", "trigger",
                                     "trigger_mailboxes", "enabled"})
    if unknown:
        raise invalid(f"cannot edit {', '.join(unknown)}")
    merged = {**job.spec["rule"], **{k: v for k, v in changes.items() if k != "enabled"}}
    r = parse_rule(merged)
    update: dict[str, Any] = {"spec": {**job.spec, "rule": r.model_dump(), "trigger": r.trigger}}
    if "enabled" in changes:
        update["status"] = ACTIVE if changes["enabled"] else PAUSED
    return update


# ------------------------------------------------------------------ triggers


def process_triggers(app: MailApp, store: JobStore, now: datetime) -> int:
    """Apply trigger rules to ``message_added`` events since each rule's cursor.

    Returns the number of batches (runs) executed. At-least-once, per rule.
    """
    batches = 0
    for job in store.find(job_type="rule", status=ACTIVE, limit=500):
        if not job.spec.get("trigger"):
            continue
        name = f"rule:{job.id}"
        cursor = store.cursor(name)
        if cursor is None:
            store.set_cursor(name, store.max_event_seq())
            continue
        for _ in range(TRIGGER_PAGES_PER_TICK):
            rows = app.db.query(
                "SELECT seq, mailbox, data_json FROM events WHERE account=? AND seq>? AND "
                "type='message_added' ORDER BY seq LIMIT ?", (job.account, cursor, TRIGGER_PAGE))
            if not rows:
                break
            try:
                batches += _trigger_batch(app, store, job, rows, now)
            except Exception:  # noqa: BLE001 - never wedge the loop on one rule
                log.exception("rule %s trigger failed", job.id)
            cursor = int(rows[-1]["seq"])
            store.set_cursor(name, cursor)
            if len(rows) < TRIGGER_PAGE:
                break
    return batches


def _trigger_batch(app: MailApp, store: JobStore, job: Job, rows: list[Any], now: datetime
                   ) -> int:
    import json

    from ..domain.models import MessageHandle

    r = parse_rule(job.spec["rule"])
    boxes = set(r.trigger_mailboxes)
    by_box: dict[tuple[str, int], list[int]] = {}
    for row in rows:
        if row["mailbox"] not in boxes:
            continue
        h = MessageHandle.parse(json.loads(row["data_json"])["handle"])
        by_box.setdefault((h.mailbox, h.uidvalidity), []).append(h.uid)
    if not by_box:
        return 0
    caller = job_caller(job.client_id)
    matched: list[str] = []
    try:
        for (mailbox, uv), uids in by_box.items():
            app.authorize_read(caller, job.account, [mailbox], kind="rules.trigger")
            try:
                summaries = app.store(job.account).fetch_summaries(mailbox, uv, uids)
            except MailError:
                continue  # mailbox reset or messages gone: nothing to act on
            matched += [s.handle for s in summaries if matches(r.match, s)]
    except MailError as exc:
        store.record_run(job.id, status="blocked", now=now,
                         detail={"trigger": True, "error": exc.to_dict()})
        return 1
    if not matched:
        return 0
    results = apply_actions(app, caller, matched, r.actions)
    store.record_run(
        job.id, status=_overall(results), now=now,
        operation_id=next((x.get("operation_id") for x in results if x.get("operation_id")), None),
        detail={"trigger": True, "matched": len(matched), "results": results})
    return 1
