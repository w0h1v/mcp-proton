"""Message reads, search and organizing/deleting writes.

Reads never change server state (the adapter uses BODY.PEEK); marking a message
read is an explicit ORGANIZE write. Every write is classified by
``effects.plan`` from the source and destination mailbox roles, goes through
``MailApp.run`` and is re-planned inside the executor: if the effects differ
from the plan recorded in the stored request (a mailbox role changed, a
destination disappeared) the operation fails with ``conflict`` instead of
running with different impact than was authorized.

Bulk writes process UIDs in chunks of at most ``CHUNK``. A failure in one
chunk (stale handle, vanished message, server rejection) fails only its items;
the rest continue, and the overall status reports partial success.
"""

from __future__ import annotations

import hashlib
import heapq
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC
from typing import Any, Literal

from ..bridge import mime
from ..domain.errors import ErrorCode, MailError, invalid, not_found, stale
from ..domain.families import OperationFamily as F
from ..domain.families import family_of
from ..domain.models import (
    ItemResult,
    MailboxRole,
    Message,
    MessageHandle,
    MessageSummary,
    OperationOutcome,
    OperationStatus,
    Page,
    SearchQuery,
    SearchResult,
)
from ..domain.requests import CallerContext, OperationRequest
from ..policy.model import Constraints
from . import effects
from .common import (
    MAX_BATCH,
    bulk_status,
    canonical_mailbox,
    canonical_mailboxes,
    decode_cursor,
    encode_cursor,
    group_by_mailbox,
    handles_payload,
    parse_handles,
)
from .core import ExecResult, MailApp, executor
from .effects import DomainOp, EffectPlan

CHUNK = 100
MAX_RAW_BYTES_DEFAULT = 25 * 1024 * 1024

SEEN = "\\Seen"
FLAGGED = "\\Flagged"
ANSWERED = "\\Answered"
DRAFT = "\\Draft"
DELETED = "\\Deleted"

# Connection-level failures abort the remaining chunks (they would all fail).
_FATAL = frozenset({ErrorCode.BRIDGE_UNAVAILABLE, ErrorCode.AUTH_FAILED, ErrorCode.TLS_ERROR})


# ---------------------------------------------------------------- cancellation

_CANCEL_LOCK = threading.Lock()
_CANCELLED: set[str] = set()


def request_cancel(op_id: str) -> None:
    """Ask a running bulk operation to stop before its next chunk.

    The remaining items are reported as ``skipped``; chunks already applied are
    not rolled back. Calling this for an operation that is not running has no
    effect once the flag is cleared at the end of execution.
    """
    with _CANCEL_LOCK:
        _CANCELLED.add(op_id)


def _cancelled(op_id: str) -> bool:
    with _CANCEL_LOCK:
        return op_id in _CANCELLED


def _clear_cancel(op_id: str) -> None:
    with _CANCEL_LOCK:
        _CANCELLED.discard(op_id)


def _chunks(items: Sequence[Any], size: int | None = None) -> Iterable[list[Any]]:
    size = size or CHUNK  # looked up at call time so tests can shrink it
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


# ---------------------------------------------------------------- read helpers


def _clamp_limit(app: MailApp, limit: int) -> int:
    return max(1, min(int(limit), app.config.max_page_size))


def _constraints(app: MailApp, caller: CallerContext) -> list[Constraints]:
    """Global constraints plus the caller's client constraints (all must hold)."""
    policy = app.policy
    cons: list[Constraints] = [policy.constraints]
    client = policy.client(caller.client_id)
    if client is not None and client.constraints is not None:
        cons.append(client.constraints)
    return cons


def release_settings(app: MailApp, caller: CallerContext) -> tuple[bool, bool]:
    """(release_bodies, release_attachments) after intersecting global and client constraints."""
    cons = _constraints(app, caller)
    return (all(c.release_bodies for c in cons), all(c.release_attachments for c in cons))


_CONTENT_CRITERIA = ("body", "text", "header")


def uses_content_criteria(query: SearchQuery) -> bool:
    """True if the query matches on message content (body, text or headers) anywhere."""
    if any(getattr(query, f) is not None for f in _CONTENT_CRITERIA):
        return True
    if query.not_ is not None and uses_content_criteria(query.not_):
        return True
    return any(uses_content_criteria(q) for q in query.any_of or [])


def require_content_search_allowed(app: MailApp, caller: CallerContext, query: SearchQuery
                                   ) -> None:
    """Content searches are an oracle for text that must not be released: refuse
    body/text/header criteria when bodies are not released to this caller."""
    if not release_settings(app, caller)[0] and uses_content_criteria(query):
        raise MailError(ErrorCode.CONSTRAINT_VIOLATION,
                        "searching message content (body, text, header) is disabled by "
                        "policy because bodies are not released")


def mailbox_names(app: MailApp, account: str) -> set[str]:
    return {m.name for m in app.store(account).list_mailboxes()}


def list_messages(app: MailApp, caller: CallerContext, account: str, mailbox: str,
                  limit: int = 50, cursor: str | None = None,
                  unread_only: bool = False) -> Page:
    """Newest first by UID. The cursor pins (UIDVALIDITY, last UID returned)."""
    mailbox = canonical_mailbox(app, account, mailbox)
    app.authorize_read(caller, account, [mailbox], kind="messages.list")
    limit = _clamp_limit(app, limit)
    store = app.store(account)
    uidvalidity, uids = store.list_uids(mailbox, ["UNSEEN"] if unread_only else None)
    cur = decode_cursor(cursor)
    ordered = sorted(uids, reverse=True)
    if cur is not None:
        if cur.get("k") != "list" or cur.get("mb") != mailbox \
                or bool(cur.get("unread")) != unread_only:
            raise invalid("cursor does not belong to this listing")
        if cur.get("uv") != uidvalidity:
            raise stale("mailbox UIDVALIDITY changed; restart the listing", mailbox=mailbox)
        ordered = [u for u in ordered if u < int(cur["last"])]
    page_uids = ordered[:limit]
    items = store.fetch_summaries(mailbox, uidvalidity, page_uids)
    next_cursor = None
    if len(ordered) > limit and page_uids:
        next_cursor = encode_cursor({"k": "list", "mb": mailbox, "uv": uidvalidity,
                                     "last": page_uids[-1], "unread": unread_only})
    return Page(items=items, total=len(uids), next_cursor=next_cursor)


def read_message(app: MailApp, caller: CallerContext, handle: str, *,
                 include_headers: bool = False, include_body: bool = True,
                 body_format: Literal["text", "html", "both"] = "both",
                 mark_read: bool = False) -> tuple[Message, OperationOutcome | None]:
    """Like :func:`get_message` but also returns the mark-read outcome (if requested),
    which may be ``pending`` when policy asks for approval."""
    h = MessageHandle.parse(handle)
    app.authorize_read(caller, h.account, [h.mailbox], kind="messages.get")
    release_bodies, _ = release_settings(app, caller)
    fetched = app.store(h.account).fetch_message(h.mailbox, h.uidvalidity, h.uid)
    message = mime.parse_message(
        fetched, h, include_headers=include_headers,
        include_body=include_body and release_bodies,
        max_body_chars=app.config.max_body_chars, body_format=body_format)
    outcome: OperationOutcome | None = None
    if mark_read:
        outcome = set_flags(app, caller, [handle], add=["read"])
        if outcome.status is OperationStatus.SUCCEEDED and SEEN not in message.flags:
            message.flags = [*message.flags, SEEN]
    return message, outcome


def get_message(app: MailApp, caller: CallerContext, handle: str, include_headers: bool = False,
                include_body: bool = True, body_format: Literal["text", "html", "both"] = "both",
                mark_read: bool = False) -> Message:
    """Fetch one message. Never sets ``\\Seen`` unless ``mark_read`` (a separate
    ORGANIZE write). The body is omitted when ``release_bodies`` is False."""
    return read_message(app, caller, handle, include_headers=include_headers,
                        include_body=include_body, body_format=body_format,
                        mark_read=mark_read)[0]


def get_raw(app: MailApp, caller: CallerContext, handle: str) -> bytes:
    """Full RFC 822 bytes. Refused unless both body and attachment release are enabled."""
    h = MessageHandle.parse(handle)
    app.authorize_read(caller, h.account, [h.mailbox], kind="messages.raw")
    bodies, attachments = release_settings(app, caller)
    if not (bodies and attachments):
        raise MailError(ErrorCode.CONSTRAINT_VIOLATION,
                        "raw message release is disabled by policy")
    fetched = app.store(h.account).fetch_message(h.mailbox, h.uidvalidity, h.uid)
    limit = min(c.max_attachment_bytes for c in _constraints(app, caller))
    if len(fetched.raw) > limit:
        raise MailError(ErrorCode.TOO_LARGE, "message exceeds the configured size limit",
                        size=len(fetched.raw), limit=limit)
    return fetched.raw


# ---------------------------------------------------------------- search


def _sort_ts(s: MessageSummary) -> float:
    dt = s.internal_date or s.date
    if dt is None:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def search(app: MailApp, caller: CallerContext, account: str, query: SearchQuery,
           mailboxes: list[str] | None = None, limit: int = 50,
           cursor: str | None = None) -> SearchResult:
    """Structured search. Default scope is INBOX only (disclosed in ``notes``);
    pass ``mailboxes`` to widen it. Mailboxes are searched sequentially and the
    results merged newest first by internal date (UID order within a mailbox).
    Completeness is always ``unknown``: Bridge sync state cannot be established."""
    default_scope = not mailboxes
    scope = canonical_mailboxes(app, account, mailboxes) if mailboxes else ["INBOX"]
    app.authorize_read(caller, account, scope, kind="messages.search")
    require_content_search_allowed(app, caller, query)
    limit = _clamp_limit(app, limit)
    store = app.store(account)
    qhash = hashlib.sha256(
        (query.model_dump_json(by_alias=True, exclude_none=True) + "|" + "\x00".join(scope))
        .encode()).hexdigest()[:16]
    cur = decode_cursor(cursor)
    if cur is not None and (cur.get("k") != "search" or cur.get("q") != qhash):
        raise invalid("cursor does not belong to this search")
    last: dict[str, int] = dict(cur.get("l", {})) if cur else {}
    pinned: dict[str, int] = dict(cur.get("v", {})) if cur else {}

    notes: list[str] = []
    total = 0
    uvs: dict[str, int] = {}
    hits: dict[str, list[int]] = {}
    for mb in scope:
        uv, uids, adapter_notes = store.search(mb, query)
        notes.extend(n for n in adapter_notes if n not in notes)
        if mb in pinned and pinned[mb] != uv:
            raise stale("mailbox UIDVALIDITY changed; restart the search", mailbox=mb)
        uvs[mb] = uv
        total += len(uids)
        ordered = sorted(uids, reverse=True)
        if mb in last:
            ordered = [u for u in ordered if u < last[mb]]
        hits[mb] = ordered

    # K-way merge of per-mailbox UID-descending streams by head timestamp.
    cands: dict[str, list[MessageSummary]] = {
        mb: store.fetch_summaries(mb, uvs[mb], hits[mb][:limit]) for mb in scope}
    pos = dict.fromkeys(scope, 0)
    heap: list[tuple[float, int, str]] = []
    for idx, mb in enumerate(scope):
        if cands[mb]:
            heapq.heappush(heap, (-_sort_ts(cands[mb][0]), idx, mb))
    items: list[MessageSummary] = []
    new_last = dict(last)
    while heap and len(items) < limit:
        _, idx, mb = heapq.heappop(heap)
        s = cands[mb][pos[mb]]
        items.append(s)
        new_last[mb] = s.uid
        pos[mb] += 1
        if pos[mb] < len(cands[mb]):
            heapq.heappush(heap, (-_sort_ts(cands[mb][pos[mb]]), idx, mb))
    more = any(u < new_last[mb] for mb in scope if mb in new_last for u in hits[mb]) or any(
        hits[mb] for mb in scope if mb not in new_last)
    next_cursor = (encode_cursor({"k": "search", "q": qhash, "v": uvs, "l": new_last})
                   if more and items else None)

    if default_scope:
        notes.append("Searched INBOX only (default scope); pass mailboxes to search elsewhere.")
    else:
        notes.append("Searched: " + ", ".join(scope) + ".")
    if len(scope) > 1:
        notes.append("Results are merged newest first by internal date; a message with several "
                     "occurrences (for example labels) can appear more than once.")
    return SearchResult(items=items, total=total, next_cursor=next_cursor, scope=scope,
                        completeness="unknown", notes=notes)


# ---------------------------------------------------------------- flag normalization

# alias -> (IMAP flag, True if the alias means "set the flag")
_FLAG_ALIASES: dict[str, tuple[str, bool]] = {
    "read": (SEEN, True), "seen": (SEEN, True), "unread": (SEEN, False), "unseen": (SEEN, False),
    "star": (FLAGGED, True), "starred": (FLAGGED, True), "flagged": (FLAGGED, True),
    "unstar": (FLAGGED, False), "unstarred": (FLAGGED, False), "unflagged": (FLAGGED, False),
    "answered": (ANSWERED, True), "unanswered": (ANSWERED, False),
}
_SETTABLE = {SEEN.lower(), FLAGGED.lower(), ANSWERED.lower(), DRAFT.lower()}
_CANONICAL = {f.lower(): f for f in (SEEN, FLAGGED, ANSWERED, DRAFT)}


def normalize_flags(add: Sequence[str] | None, remove: Sequence[str] | None
                    ) -> tuple[list[str], list[str]]:
    """Map friendly names (read/unread/star/unstar...) to IMAP flags.

    ``add=["unread"]`` clears ``\\Seen``; ``remove=["unread"]`` sets it. ``\\Deleted``
    is refused here: deletion is a PERMANENT_DELETE operation (``mark_deleted``).
    """
    to_add: list[str] = []
    to_remove: list[str] = []

    def put(name: str, adding: bool) -> None:
        key = name.strip().lower()
        if key in ("deleted", DELETED.lower()):
            raise invalid("use mark_deleted/clear_deleted for \\Deleted")
        if key in _FLAG_ALIASES:
            flag, positive = _FLAG_ALIASES[key]
            adding = adding == positive
        elif key in _SETTABLE:
            flag = _CANONICAL[key]
        elif key.lstrip("\\") in {f.lstrip("\\").lower() for f in _SETTABLE}:
            flag = _CANONICAL["\\" + key.lstrip("\\")]
        else:
            raise invalid(f"unsupported flag {name!r}; use read, unread, star, unstar, "
                          "answered or draft")
        target = to_add if adding else to_remove
        if flag not in target:
            target.append(flag)

    for n in add or []:
        put(n, True)
    for n in remove or []:
        put(n, False)
    if not to_add and not to_remove:
        raise invalid("no flag changes requested")
    if set(to_add) & set(to_remove):
        raise invalid("a flag cannot be both added and removed")
    return to_add, to_remove


# ---------------------------------------------------------------- write specs


@dataclass(frozen=True)
class _Spec:
    kind: str
    op: DomainOp
    template: str
    family: F = F.ORGANIZE
    dest_role: MailboxRole | None = None  # destination resolved by role
    dest_default: str | None = None  # destination when the caller passes none
    dest_required: bool = False  # caller must pass a destination mailbox (or label)
    uses_flags: bool = False


_SPEC_LIST = (
    _Spec("messages.flags", DomainOp.FLAGS, "Update flags ({flags}) on {n} from {src}",
          uses_flags=True),
    _Spec("messages.move", DomainOp.MOVE, "Move {n} from {src} to {dest}", dest_required=True),
    _Spec("messages.archive", DomainOp.ARCHIVE, "Archive {n} from {src}",
          dest_role=MailboxRole.ARCHIVE),
    _Spec("messages.trash", DomainOp.TRASH, "Move {n} from {src} to Trash",
          dest_role=MailboxRole.TRASH),
    _Spec("messages.restore", DomainOp.RESTORE, "Restore {n} from {src} to {dest}",
          dest_default="INBOX"),
    _Spec("messages.spam", DomainOp.SPAM, "Move {n} from {src} to Spam",
          dest_role=MailboxRole.SPAM),
    _Spec("messages.not_spam", DomainOp.NOT_SPAM, "Mark {n} from {src} as not spam ({dest})",
          dest_default="INBOX"),
    _Spec("labels.apply", DomainOp.LABEL_ADD, "Apply label {dest} to {n} from {src}",
          dest_required=True),
    _Spec("labels.remove", DomainOp.LABEL_REMOVE, "Remove label {src} from {n}"),
    _Spec("messages.mark_deleted", DomainOp.MARK_DELETED, "Mark {n} from {src} as deleted",
          family=F.PERMANENT_DELETE),
    _Spec("messages.clear_deleted", DomainOp.CLEAR_DELETED,
          "Clear the deleted mark on {n} from {src}"),
    _Spec("messages.expunge", DomainOp.EXPUNGE, "Permanently delete {n} from {src}",
          family=F.PERMANENT_DELETE),
)
_SPECS: dict[str, _Spec] = {s.kind: s for s in _SPEC_LIST}

# Public operation names accepted by bulk_preview.
OPERATIONS: dict[str, str] = {
    "flags": "messages.flags", "move": "messages.move", "archive": "messages.archive",
    "trash": "messages.trash", "restore": "messages.restore", "spam": "messages.spam",
    "not_spam": "messages.not_spam", "label_apply": "labels.apply",
    "label_remove": "labels.remove", "mark_deleted": "messages.mark_deleted",
    "clear_deleted": "messages.clear_deleted", "expunge": "messages.expunge",
}


@dataclass
class _SourcePlan:
    role: MailboxRole
    plan: EffectPlan | None = None
    error: MailError | None = None


def _plural(n: int) -> str:
    return f"{n} message" + ("" if n == 1 else "s")


def _resolve_label(names: set[str], label: str) -> str:
    full = label if label.startswith("Labels/") else f"Labels/{label}"
    if full not in names:
        raise not_found("label does not exist")
    return full


def _resolve_dest(app: MailApp, account: str, spec: _Spec, dest: str | None) -> str | None:
    """Destination mailbox for ``spec`` (None if the operation has none)."""
    store = app.store(account)
    if spec.dest_role is not None:
        name = store.mailbox_for_role(spec.dest_role)
        if name is None:
            raise not_found(f"no {spec.dest_role.value} mailbox on this account")
        return name
    if spec.op is DomainOp.LABEL_ADD:
        if not dest:
            raise invalid("a label is required")
        return _resolve_label(mailbox_names(app, account), dest)
    if spec.dest_required or spec.dest_default:
        name = dest or spec.dest_default
        if not name:
            raise invalid("a destination mailbox is required")
        return canonical_mailbox(app, account, name)
    return None


def _plan_sources(app: MailApp, account: str, spec: _Spec, sources: Iterable[str],
                  dest: str | None) -> tuple[dict[str, _SourcePlan], MailboxRole | None]:
    """Classify each source mailbox (skipping a source equal to the destination)."""
    store = app.store(account)
    dest_role = store.role_of(dest) if dest else None
    out: dict[str, _SourcePlan] = {}
    for mb in sources:
        if mb == dest:
            continue
        role = store.role_of(mb)
        sp = _SourcePlan(role)
        try:
            sp.plan = effects.plan(spec.op, role, dest_role)
        except MailError as exc:
            sp.error = exc
        out[mb] = sp
    return out, dest_role


def _strict(plans: dict[str, _SourcePlan]) -> dict[str, EffectPlan]:
    strict: dict[str, EffectPlan] = {}
    for sp in plans.values():
        if sp.error is not None:
            raise sp.error
    for mb, sp in plans.items():
        assert sp.plan is not None
        strict[mb] = sp.plan
    return strict


def _src_label(sources: Sequence[str]) -> str:
    names = sorted(set(sources))
    return ", ".join(names) if len(names) <= 2 else f"{len(names)} mailboxes"


def _build(app: MailApp, spec: _Spec, handles: Sequence[MessageHandle], *,
           dest: str | None = None, add: list[str] | None = None,
           remove: list[str] | None = None) -> OperationRequest:
    account = handles[0].account
    resolved = _resolve_dest(app, account, spec, dest)
    sources = list(group_by_mailbox(handles))
    source_names = list(dict.fromkeys(mb for mb, _ in sources))
    plans, dest_role = _plan_sources(app, account, spec, source_names, resolved)
    if not plans:
        raise invalid("all messages are already in the destination mailbox")
    strict = _strict(plans)
    also = sorted({f.value for p in strict.values() for f in p.also_families} - {spec.family.value})
    payload: dict[str, Any] = {
        "handles": handles_payload(handles),
        "dest": resolved,
        "dest_role": dest_role.value if dest_role else None,
        "plan": {mb: {"role": plans[mb].role.value, "effects": [e.value for e in p.effects],
                      "also": [f.value for f in p.also_families]}
                 for mb, p in strict.items()},
        "_also_families": also,
    }
    flag_text = ""
    if spec.uses_flags:
        payload["add"], payload["remove"] = list(add or []), list(remove or [])
        flag_text = ",".join([f"+{f}" for f in payload["add"]]
                             + [f"-{f}" for f in payload["remove"]])
    elif spec.op is DomainOp.MARK_DELETED:
        payload["add"], payload["remove"] = [DELETED], []
    elif spec.op is DomainOp.CLEAR_DELETED:
        payload["add"], payload["remove"] = [], [DELETED]
    mailboxes = list(dict.fromkeys([*source_names, *([resolved] if resolved else [])]))
    summary = spec.template.format(n=_plural(len(handles)), src=_src_label(source_names),
                                   dest=resolved, flags=flag_text)
    family = family_of(spec.kind) or spec.family
    return OperationRequest(kind=spec.kind, family=family, account=account, mailboxes=mailboxes,
                            targets=list(handles), batch_size=len(handles), payload=payload,
                            summary=summary)


def _submit(app: MailApp, caller: CallerContext, kind: str, tokens: Iterable[str], *,
            dest: str | None = None, add: list[str] | None = None,
            remove: list[str] | None = None) -> OperationOutcome:
    handles = parse_handles(tokens)
    req = _build(app, _SPECS[kind], handles, dest=dest, add=add, remove=remove)
    return app.run(caller, req)


# ---------------------------------------------------------------- public writes


def set_flags(app: MailApp, caller: CallerContext, handles: Iterable[str],
              add: Sequence[str] | None = None, remove: Sequence[str] | None = None
              ) -> OperationOutcome:
    """Add/remove flags. Friendly names: read, unread, star, unstar, answered, draft."""
    to_add, to_remove = normalize_flags(add, remove)
    return _submit(app, caller, "messages.flags", handles, add=to_add, remove=to_remove)


def move(app: MailApp, caller: CallerContext, handles: Iterable[str], dest: str
         ) -> OperationOutcome:
    return _submit(app, caller, "messages.move", handles, dest=dest)


def archive(app: MailApp, caller: CallerContext, handles: Iterable[str]) -> OperationOutcome:
    return _submit(app, caller, "messages.archive", handles)


def trash(app: MailApp, caller: CallerContext, handles: Iterable[str]) -> OperationOutcome:
    return _submit(app, caller, "messages.trash", handles)


def restore(app: MailApp, caller: CallerContext, handles: Iterable[str],
            dest: str | None = None) -> OperationOutcome:
    """Move out of Trash/Spam; ``dest`` defaults to INBOX."""
    return _submit(app, caller, "messages.restore", handles, dest=dest)


def spam(app: MailApp, caller: CallerContext, handles: Iterable[str]) -> OperationOutcome:
    """Move to Spam. Does not block the sender or create filters."""
    return _submit(app, caller, "messages.spam", handles)


def not_spam(app: MailApp, caller: CallerContext, handles: Iterable[str],
             dest: str | None = None) -> OperationOutcome:
    return _submit(app, caller, "messages.not_spam", handles, dest=dest)


def label_apply(app: MailApp, caller: CallerContext, handles: Iterable[str], label: str
                ) -> OperationOutcome:
    """Copy the messages into a label mailbox (``Labels/<name>`` or just ``<name>``)."""
    return _submit(app, caller, "labels.apply", handles, dest=label)


def label_remove(app: MailApp, caller: CallerContext, handles: Iterable[str]
                 ) -> OperationOutcome:
    """Remove the label occurrences identified by ``handles`` (handles must point into a
    label mailbox). The message's other occurrences, such as its INBOX copy, remain."""
    return _submit(app, caller, "labels.remove", handles)


def mark_deleted(app: MailApp, caller: CallerContext, handles: Iterable[str]
                 ) -> OperationOutcome:
    return _submit(app, caller, "messages.mark_deleted", handles)


def clear_deleted(app: MailApp, caller: CallerContext, handles: Iterable[str]
                  ) -> OperationOutcome:
    return _submit(app, caller, "messages.clear_deleted", handles)


def expunge(app: MailApp, caller: CallerContext, handles: Iterable[str]) -> OperationOutcome:
    """Permanently delete exactly these messages (Trash/Spam only). Other messages that
    happen to carry ``\\Deleted`` are untouched (targeted UID EXPUNGE)."""
    return _submit(app, caller, "messages.expunge", handles)


# ---------------------------------------------------------------- executors

_Out = dict[str, tuple[ItemResult, dict[str, Any] | None]]


def _verify_plan(app: MailApp, spec: _Spec, payload: dict[str, Any], account: str,
                 sources: Iterable[str]) -> None:
    """Re-plan at execution time; any difference from the authorized plan is a conflict."""
    changed = MailError(ErrorCode.CONFLICT, "effects changed; request again")
    dest = payload.get("dest")
    if dest and dest not in mailbox_names(app, account):
        raise MailError(ErrorCode.CONFLICT, "destination mailbox no longer exists; "
                        "request again")
    plans, dest_role = _plan_sources(app, account, spec, sources, dest)
    if (dest_role.value if dest_role else None) != payload.get("dest_role"):
        raise changed
    recorded: dict[str, Any] = payload.get("plan", {})
    if set(plans) != set(recorded):
        raise changed
    for mb, sp in plans.items():
        if sp.plan is None:
            raise changed
        rec = recorded[mb]
        if (sp.role.value != rec["role"]
                or [e.value for e in sp.plan.effects] != rec["effects"]
                or [f.value for f in sp.plan.also_families] != rec["also"]):
            raise changed


def _handle_token(account: str, mailbox: str, uidvalidity: int | None, uid: int | None
                  ) -> str | None:
    if uidvalidity is None or uid is None:
        return None
    return MessageHandle(account=account, mailbox=mailbox, uidvalidity=uidvalidity,
                         uid=uid).token()


def _ok(h: MessageHandle, prior: dict[str, Any] | None, new: str | None = None,
        detail: str | None = None) -> tuple[ItemResult, dict[str, Any] | None]:
    return ItemResult(target=h.token(), status="succeeded", new_handle=new, detail=detail), prior


def _fail(h: MessageHandle, detail: str) -> tuple[ItemResult, dict[str, Any] | None]:
    return ItemResult(target=h.token(), status="failed", detail=detail), None


def _skip(h: MessageHandle, detail: str) -> tuple[ItemResult, dict[str, Any] | None]:
    return ItemResult(target=h.token(), status="skipped", detail=detail), None


def _do_chunk(app: MailApp, spec: _Spec, payload: dict[str, Any], mailbox: str, uv: int,
              chunk: list[MessageHandle], dest: str | None) -> tuple[_Out, str | None]:
    """Apply one chunk. Returns per-item results and, if a connection-level failure
    stopped the chunk, its error code (the caller then skips the remaining chunks)."""
    store = app.store(chunk[0].account)
    account = chunk[0].account
    flags = store.fetch_flags(mailbox, uv, [h.uid for h in chunk])  # raises stale_handle
    out: _Out = {}
    live: list[MessageHandle] = []
    for h in chunk:
        if h.uid in flags:
            live.append(h)
        else:
            out[h.token()] = _fail(h, "not_found")
    if not live:
        return out, None
    uids = [h.uid for h in live]
    op = spec.op

    def prior(h: MessageHandle, **extra: Any) -> dict[str, Any]:
        return {"op": spec.kind, "mailbox": mailbox, "uidvalidity": uv, "uid": h.uid,
                "flags": flags[h.uid], **extra}

    try:
        if op in (DomainOp.MOVE, DomainOp.ARCHIVE, DomainOp.TRASH, DomainOp.RESTORE,
                  DomainOp.SPAM, DomainOp.NOT_SPAM):
            assert dest is not None
            res = store.move(mailbox, uv, uids, dest)
            for h in live:
                new = _handle_token(account, dest, res.dest_uidvalidity, res.mapping.get(h.uid))
                out[h.token()] = _ok(h, prior(h, dest=dest, new_handle=new), new)
        elif op is DomainOp.LABEL_ADD:
            assert dest is not None
            res = store.copy(mailbox, uv, uids, dest)
            for h in live:
                new = _handle_token(account, dest, res.dest_uidvalidity, res.mapping.get(h.uid))
                out[h.token()] = _ok(h, prior(h, label=dest, new_handle=new), new)
        elif op in (DomainOp.FLAGS, DomainOp.MARK_DELETED, DomainOp.CLEAR_DELETED):
            store.store_flags(mailbox, uv, uids, add=payload.get("add") or None,
                              remove=payload.get("remove") or None)
            for h in live:
                out[h.token()] = _ok(h, prior(h))
        elif op in (DomainOp.EXPUNGE, DomainOp.LABEL_REMOVE):
            extra: dict[str, dict[str, Any]] = {}
            if op is DomainOp.LABEL_REMOVE:
                ids = {s.uid: s.message_id for s in store.fetch_summaries(mailbox, uv, uids)}
                extra = {h.token(): {"label": mailbox, "message_id": ids.get(h.uid)}
                         for h in live}
            # UID EXPUNGE only removes messages carrying \Deleted, so flag exactly the targets.
            store.store_flags(mailbox, uv, uids, add=[DELETED])
            gone = set(store.expunge_uids(mailbox, uv, uids))
            for h in live:
                if h.uid in gone:
                    p = prior(h, **extra[h.token()]) if op is DomainOp.LABEL_REMOVE else None
                    out[h.token()] = _ok(h, p)
                else:
                    out[h.token()] = _fail(h, "not_expunged")
        else:  # pragma: no cover - specs are exhaustive
            raise MailError(ErrorCode.INTERNAL, f"no executor for {op.value}")
    except MailError as exc:
        if op in (DomainOp.EXPUNGE, DomainOp.LABEL_REMOVE, DomainOp.MOVE, DomainOp.ARCHIVE,
                  DomainOp.TRASH, DomainOp.RESTORE, DomainOp.SPAM, DomainOp.NOT_SPAM):
            # The command may have partly applied; report what is observable.
            try:
                still = set(store.fetch_flags(mailbox, uv, uids))
            except MailError:
                still = set(uids)
            for h in live:
                if h.token() in out:
                    continue
                note = exc.code.value if h.uid in still else \
                    f"{exc.code.value}; outcome uncertain, message is no longer in the source"
                out[h.token()] = _fail(h, note)
        else:
            for h in live:
                out.setdefault(h.token(), _fail(h, exc.code.value))
        return out, (exc.code.value if exc.code in _FATAL else None)
    return out, None


def _execute_batch(app: MailApp, rec: Any) -> ExecResult:
    spec = _SPECS[rec.kind]
    payload: dict[str, Any] = rec.request.payload
    handles = [MessageHandle.parse(t) for t in payload["handles"]]
    account = handles[0].account
    dest: str | None = payload.get("dest")
    groups = group_by_mailbox(handles)
    results: _Out = {}
    aborted: str | None = None
    was_cancelled = False
    try:
        _verify_plan(app, spec, payload, account, dict.fromkeys(mb for mb, _ in groups))
        for (mailbox, uv), hs in groups.items():
            if mailbox == dest:
                for h in hs:
                    results[h.token()] = _skip(h, "already in destination")
                continue
            for chunk in _chunks(hs):
                if aborted is not None:
                    for h in chunk:
                        results[h.token()] = _skip(h, f"aborted: {aborted}")
                    continue
                if _cancelled(rec.id):
                    was_cancelled = True
                    for h in chunk:
                        results[h.token()] = _skip(h, "cancelled")
                    continue
                try:
                    chunk_results, fatal = _do_chunk(app, spec, payload, mailbox, uv, chunk, dest)
                except MailError as exc:  # stale handle, mailbox gone, ...
                    for h in chunk:
                        results[h.token()] = _fail(h, exc.code.value)
                    aborted = exc.code.value if exc.code in _FATAL else None
                else:
                    results.update(chunk_results)
                    aborted = fatal
    finally:
        _clear_cancel(rec.id)
    items: list[ItemResult] = []
    priors: list[dict[str, Any] | None] = []
    for h in handles:
        item, prior = results.get(h.token(), _skip(h, "not processed"))
        items.append(item)
        priors.append(prior)
    counts = {s: sum(1 for i in items if i.status == s) for s in ("succeeded", "failed", "skipped")}
    result: dict[str, Any] = {"counts": counts}
    if dest:
        result["dest"] = dest
    if was_cancelled:
        result["cancelled"] = True
    return ExecResult(status=OperationStatus(bulk_status(items)), result=result, items=items,
                      prior_states=priors)


def _register(spec: _Spec) -> None:
    executor(spec.kind, spec.family)(_execute_batch)


for _spec in _SPEC_LIST:
    _register(_spec)


# ---------------------------------------------------------------- bulk preview


def _preview_handles(app: MailApp, account: str, query: SearchQuery,
                     mailboxes: list[str] | None, cap: int) -> tuple[list[MessageHandle], bool]:
    store = app.store(account)
    scope = list(dict.fromkeys(mailboxes)) if mailboxes else ["INBOX"]
    out: list[MessageHandle] = []
    truncated = False
    for mb in scope:
        uv, uids, _ = store.search(mb, query)
        for uid in sorted(uids, reverse=True):
            if len(out) >= cap:
                truncated = True
                break
            out.append(MessageHandle(account=account, mailbox=mb, uidvalidity=uv, uid=uid))
    return out, truncated


def bulk_preview(app: MailApp, caller: CallerContext, account: str, operation: str, *,
                 handles: Iterable[str] | None = None, query: SearchQuery | None = None,
                 mailboxes: list[str] | None = None, dest: str | None = None,
                 add: Sequence[str] | None = None, remove: Sequence[str] | None = None,
                 limit: int = MAX_BATCH) -> dict[str, Any]:
    """Read-only preview of a bulk write: nothing is journaled or changed.

    Select targets with ``handles`` or with a ``query`` over ``mailboxes`` (default
    INBOX). Returns occurrences grouped by mailbox, a *heuristic* count of unique
    messages (Message-ID equality), the effect plan per mailbox, and the effective
    policy action for the exact request a real write would make.
    """
    if operation not in OPERATIONS:
        raise invalid(f"unknown operation {operation!r}", operations=sorted(OPERATIONS))
    if (handles is None) == (query is None):
        raise invalid("pass exactly one of handles or query")
    spec = _SPECS[OPERATIONS[operation]]
    cap = max(1, min(int(limit), MAX_BATCH))
    truncated = False
    if handles is not None:
        parsed = parse_handles(handles, account=account, max_items=MAX_BATCH)
    else:
        assert query is not None
        scope = canonical_mailboxes(app, account, mailboxes) if mailboxes else ["INBOX"]
        app.authorize_read(caller, account, scope, kind="bulk.preview")
        require_content_search_allowed(app, caller, query)
        parsed, truncated = _preview_handles(app, account, query, scope, cap)
        if not parsed:
            return {"account": account, "operation": operation, "occurrences": 0,
                    "by_mailbox": [], "unique_messages": {"count": 0, "certainty": "heuristic",
                                                          "basis": "Message-ID"},
                    "policy": None, "truncated": False, "summary": "No matching messages."}
    groups = group_by_mailbox(parsed)
    names = list(dict.fromkeys(mb for mb, _ in groups))
    app.authorize_read(caller, account, names, kind="bulk.preview")

    if spec.uses_flags:
        to_add, to_remove = normalize_flags(add, remove)
    else:
        to_add, to_remove = [], []
    resolved = _resolve_dest(app, account, spec, dest)
    plans, _ = _plan_sources(app, account, spec, names, resolved)
    store = app.store(account)

    seen_ids: set[str] = set()
    unique = 0
    stale_groups = 0
    by_mailbox: list[dict[str, Any]] = []
    for (mb, uv), hs in groups.items():
        entry: dict[str, Any] = {"mailbox": mb, "uidvalidity": uv, "count": len(hs),
                                 "role": store.role_of(mb).value}
        sp = plans.get(mb)
        if mb == resolved:
            entry["note"] = "already in destination; would be skipped"
        elif sp is not None and sp.plan is not None:
            p = sp.plan
            entry["plan"] = {"effects": [e.value for e in p.effects], "family": p.family.value,
                             "also_families": [f.value for f in p.also_families],
                             "reversible": p.reversible, "verified": p.verified,
                             "notes": list(p.notes)}
        elif sp is not None and sp.error is not None:
            entry["error"] = sp.error.to_dict()
        try:
            for chunk in _chunks(hs):
                for s in store.fetch_summaries(mb, uv, [h.uid for h in chunk]):
                    key = (s.message_id or "").strip()
                    if not key:
                        unique += 1
                    elif key not in seen_ids:
                        seen_ids.add(key)
                        unique += 1
        except MailError as exc:
            if exc.code is not ErrorCode.STALE_HANDLE:
                raise
            stale_groups += 1
            entry["error"] = exc.to_dict()
        by_mailbox.append(entry)

    policy: dict[str, Any] | None = None
    if all(sp.plan is not None for sp in plans.values()) and plans:
        req = _build(app, spec, parsed, dest=dest, add=to_add, remove=to_remove)
        d = app.decide(caller, req)
        policy = {"action": d.action.value, "family": d.family.value, "reasons": d.reasons,
                  "violation": d.violation, "code": d.code, "summary": req.summary}
    return {
        "account": account, "operation": operation, "occurrences": len(parsed),
        "by_mailbox": by_mailbox,
        "unique_messages": {"count": unique, "certainty": "heuristic", "basis": "Message-ID",
                            "note": "Messages without a Message-ID count individually; the "
                                    "same message in several mailboxes (labels) counts once."},
        "policy": policy, "stale_mailboxes": stale_groups, "truncated": truncated,
        "summary": f"{operation} on {_plural(len(parsed))}",
    }


__all__ = [
    "CHUNK", "OPERATIONS", "archive", "bulk_preview", "clear_deleted", "expunge", "get_message",
    "get_raw", "label_apply", "label_remove", "list_messages", "mailbox_names", "mark_deleted",
    "move", "normalize_flags", "not_spam", "read_message", "release_settings", "request_cancel",
    "restore", "search", "set_flags", "spam", "trash",
]
