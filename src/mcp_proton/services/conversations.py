"""Conversation grouping from Message-ID, References and In-Reply-To.

Presentation only: this does not match Proton's own conversation grouping
exactly and must never be used to pick destructive targets. The same logical
message can be visible in several mailboxes (folder, labels, All Mail) under
different UIDs; those are grouped as *occurrences* of one node.

``build_threads`` is a pure function over parsed headers. ``get_conversation``
gathers headers from the mailboxes through the adapter (bounded fixed-point
search over referenced ids) and delegates to it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import Field

from ..bridge.mime import parse_message
from ..bridge.ports import MailStore
from ..domain.errors import MailError
from ..domain.families import OperationFamily, register_kind
from ..domain.models import Address, MailboxRole, MessageHandle, Model, SearchQuery
from ..domain.requests import CallerContext
from .common import parse_handles
from .core import MailApp

KIND_GET = register_kind("conversations.get", OperationFamily.READ)

NOTE = ("presentation only; does not match Proton's conversation grouping exactly; "
        "never use for destructive targeting")
HEURISTIC_WINDOW = timedelta(days=30)
MAX_ROUNDS = 4  # fixed-point iterations of the Message-ID search
MAX_IDS_PER_ROUND = 40  # Message-IDs searched per round and mailbox
_ID_RE = re.compile(r"<([^<>\s]+)>")
_PREFIX_RE = re.compile(r"^\s*((re|fwd?|aw|sv|antw)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)


# ---------------------------------------------------------------- models


def normalize_id(value: str | None) -> str | None:
    """Canonical comparison form of a Message-ID: no brackets, lower case."""
    if not value:
        return None
    m = _ID_RE.search(value)
    out = (m.group(1) if m else value).strip().lower()
    return out or None


def normalize_subject(subject: str | None) -> str:
    """Subject without "Re:/Fwd:" prefixes, collapsed whitespace, lower case."""
    text = _PREFIX_RE.sub("", subject or "")
    return " ".join(text.split()).lower()


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


@dataclass(frozen=True)
class HeaderInfo:
    """The threading-relevant headers of one mailbox occurrence."""

    handle: str
    mailbox: str
    uid: int
    message_id: str | None = None
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    subject: str | None = None
    date: datetime | None = None
    from_: tuple[Address, ...] = field(default_factory=tuple)


class Occurrence(Model):
    handle: str
    mailbox: str
    uid: int


class ConversationNode(Model):
    handle: str  # representative occurrence
    parent: str | None = None  # handle of the parent node's representative occurrence
    message_id: str | None = None
    date: datetime | None = None
    subject: str | None = None
    from_: list[Address] = Field(default_factory=list, alias="from")
    heuristic: bool = False  # parent link came from the subject heuristic
    occurrence_count: int = 1


@dataclass
class ThreadResult:
    nodes: list[ConversationNode]
    occurrences: dict[str, list[Occurrence]]
    incomplete: bool
    missing: list[str]
    method: Literal["headers", "subject_heuristic", "mixed"]


class Conversation(Model):
    seed: str
    nodes: list[ConversationNode]
    occurrences: dict[str, list[Occurrence]]
    incomplete: bool
    missing_message_ids: list[str] = Field(default_factory=list)
    method: Literal["headers", "subject_heuristic", "mixed"]
    heuristic: bool
    searched_mailboxes: list[str]
    truncated: bool = False
    note: str = NOTE


# ---------------------------------------------------------------- pure threading


def build_threads(headers: list[HeaderInfo]) -> ThreadResult:
    """Group occurrences into logical messages and link them into a thread forest.

    Parent = In-Reply-To when known, else the last known References entry. Messages
    without any threading header are attached by normalized subject within 30 days
    (flagged ``heuristic``). ``incomplete`` is set when a referenced id is unknown.
    """
    # 1. logical messages: one per Message-ID, several occurrences each
    groups: dict[str, list[HeaderInfo]] = {}
    for h in headers:
        key = normalize_id(h.message_id) or f"anon:{h.handle}"
        bucket = groups.setdefault(key, [])
        if all(o.handle != h.handle for o in bucket):
            bucket.append(h)
    rep = {key: occ[0] for key, occ in groups.items()}  # first occurrence represents it

    # 2. header-based parents
    parent: dict[str, str] = {}
    missing: dict[str, None] = {}
    for key, h in rep.items():
        refs = [r for r in (normalize_id(x) for x in h.references) if r]
        irt = normalize_id(h.in_reply_to)
        candidates = ([irt] if irt else []) + list(reversed(refs))
        for cand in candidates:
            if cand != key and cand in groups and not _creates_cycle(parent, key, cand):
                parent[key] = cand
                break
        for ref in [*refs, *([irt] if irt else [])]:
            if ref != key and ref not in groups:
                missing.setdefault(ref)

    # 3. subject heuristic for messages with no threading headers at all
    heuristic_keys: set[str] = set()
    ordered = sorted(rep, key=lambda k: (_utc(rep[k].date) or datetime.max.replace(tzinfo=UTC),
                                         rep[k].uid))
    for i, key in enumerate(ordered):
        h = rep[key]
        if key in parent or h.in_reply_to or h.references:
            continue
        subject = normalize_subject(h.subject)
        date = _utc(h.date)
        if not subject or date is None:
            continue
        for cand in reversed(ordered[:i]):  # the closest earlier message first
            c = rep[cand]
            cdate = _utc(c.date)
            if cdate is None or normalize_subject(c.subject) != subject:
                continue
            if date - cdate <= HEURISTIC_WINDOW and not _creates_cycle(parent, key, cand):
                parent[key] = cand
                heuristic_keys.add(key)
                break

    # 4. output
    nodes = []
    for key in ordered:
        h = rep[key]
        pk = parent.get(key)
        nodes.append(ConversationNode(
            handle=h.handle, parent=rep[pk].handle if pk else None, message_id=h.message_id,
            date=h.date, subject=h.subject, from_=list(h.from_),  # type: ignore[call-arg]
            heuristic=key in heuristic_keys, occurrence_count=len(groups[key])))
    occurrences = {
        (normalize_id(rep[key].message_id) or rep[key].handle): [
            Occurrence(handle=o.handle, mailbox=o.mailbox, uid=o.uid) for o in occ]
        for key, occ in groups.items()}
    header_links = len(parent) - len(heuristic_keys)
    if not heuristic_keys:
        method: Literal["headers", "subject_heuristic", "mixed"] = "headers"
    elif header_links == 0:
        method = "subject_heuristic"
    else:
        method = "mixed"
    return ThreadResult(nodes, occurrences, bool(missing), list(missing), method)


def _creates_cycle(parent: dict[str, str], child: str, new_parent: str) -> bool:
    seen = {child}
    cur: str | None = new_parent
    while cur is not None:
        if cur in seen:
            return True
        seen.add(cur)
        cur = parent.get(cur)
    return False


# ---------------------------------------------------------------- gathering


class _Gatherer:
    """Bounded header collection for one conversation."""

    def __init__(self, store: MailStore, account: str, mailboxes: list[str], limit: int) -> None:
        self.store = store
        self.account = account
        self.mailboxes = mailboxes
        self.limit = limit
        self.headers: dict[str, HeaderInfo] = {}  # handle token -> headers
        self.truncated = False
        self._seen: set[tuple[str, int]] = set()  # fetched (mailbox, uid)
        self._searched: set[tuple[str, str]] = set()  # (query kind, normalized id)

    def add_message(self, mailbox: str, uidvalidity: int, uid: int, *, strict: bool = False
                    ) -> HeaderInfo | None:
        if (mailbox, uid) in self._seen:
            return None
        if len(self.headers) >= self.limit:
            self.truncated = True
            return None
        self._seen.add((mailbox, uid))
        try:
            fetched = self.store.fetch_message(mailbox, uidvalidity, uid, header_only=True)
        except MailError:
            if strict:
                raise  # the seed must exist and its handle must be current
            return None  # expunged or mailbox changed meanwhile
        handle = MessageHandle(account=self.account, mailbox=mailbox,
                               uidvalidity=uidvalidity, uid=uid)
        msg = parse_message(fetched, handle, include_headers=False, include_body=False,
                            max_body_chars=0)
        info = HeaderInfo(
            handle=handle.token(), mailbox=mailbox, uid=uid, message_id=msg.message_id,
            in_reply_to=msg.in_reply_to, references=tuple(msg.references), subject=msg.subject,
            date=msg.date or fetched.internal_date, from_=tuple(msg.from_))
        self.headers[info.handle] = info
        return info

    def referenced_ids(self) -> set[str]:
        out: set[str] = set()
        for h in self.headers.values():
            for raw in (h.message_id, h.in_reply_to, *h.references):
                n = normalize_id(raw)
                if n:
                    out.add(n)
        return out

    def expand(self) -> None:
        """Fixed point (bounded): fetch messages for every known id and every message
        that mentions a known id in References / In-Reply-To."""
        for _ in range(MAX_ROUNDS):
            before = len(self.headers)
            ids = sorted(self.referenced_ids())
            budget = MAX_IDS_PER_ROUND
            for mid in ids:
                if budget <= 0:
                    break
                todo = [kind for kind in ("id", "refs", "irt") if (kind, mid) not in self._searched]
                if not todo:
                    continue
                budget -= 1
                for kind in todo:
                    self._searched.add((kind, mid))
                    for mailbox in self.mailboxes:
                        self._search(mailbox, kind, mid)
            if len(self.headers) == before:
                return

    def _search(self, mailbox: str, kind: str, mid: str) -> None:
        wrapped = f"<{mid}>"
        header = {"id": "Message-ID", "refs": "References", "irt": "In-Reply-To"}[kind]
        try:
            uidvalidity, uids = self.store.list_uids(mailbox, ["HEADER", header, wrapped])
        except MailError:
            return  # unreadable or missing mailbox: the thread is reported incomplete
        for uid in uids:
            self.add_message(mailbox, uidvalidity, uid)

    def subject_search(self, seed: HeaderInfo) -> None:
        subject = normalize_subject(seed.subject)
        date = _utc(seed.date)
        if not subject or date is None:
            return
        query = SearchQuery(subject=subject, since=date - HEURISTIC_WINDOW,
                            before=date + HEURISTIC_WINDOW + timedelta(days=1))
        for mailbox in self.mailboxes:
            try:
                uidvalidity, uids, _ = self.store.search(mailbox, query)
            except MailError:
                continue
            for uid in uids:
                info = self.add_message(mailbox, uidvalidity, uid)
                if info is not None and normalize_subject(info.subject) != subject:
                    self.headers.pop(info.handle)  # a substring match, not the same subject


def _default_mailboxes(app: MailApp, caller: CallerContext, account: str, seed_mailbox: str
                       ) -> list[str]:
    store = app.store(account)
    all_mail = store.mailbox_for_role(MailboxRole.ALL_MAIL)
    if all_mail:
        candidates = [all_mail]
    else:
        sent = store.mailbox_for_role(MailboxRole.SENT)
        candidates = [seed_mailbox, *([sent] if sent else [])]
    out = []
    for mb in dict.fromkeys(candidates):
        try:
            app.authorize_read(caller, account, [mb], kind=KIND_GET)
        except MailError:
            continue  # unreadable mailboxes are skipped silently for the default scope
        out.append(mb)
    return out


def _has_threading_headers(h: HeaderInfo) -> bool:
    return bool(h.in_reply_to or h.references)


def get_conversation(app: MailApp, caller: CallerContext, handle: str,
                     mailboxes: list[str] | None = None, limit: int = 100) -> Conversation:
    """Thread containing the message ``handle`` (presentation only)."""
    [seed_handle] = parse_handles([handle])
    account = seed_handle.account
    app.authorize_read(caller, account, [seed_handle.mailbox], kind=KIND_GET)
    limit = max(1, min(int(limit), 500))
    if mailboxes:
        scope = list(dict.fromkeys(mailboxes))
        app.authorize_read(caller, account, scope, kind=KIND_GET)
    else:
        scope = _default_mailboxes(app, caller, account, seed_handle.mailbox)

    store = app.store(account)
    gatherer = _Gatherer(store, account, scope, limit)
    seed = gatherer.add_message(seed_handle.mailbox, seed_handle.uidvalidity, seed_handle.uid,
                                strict=True)
    assert seed is not None
    gatherer.expand()

    if not _has_threading_headers(seed) and (not seed.message_id or len(gatherer.headers) == 1):
        gatherer.subject_search(seed)  # headers link nothing: heuristic fallback

    result = build_threads(list(gatherer.headers.values()))
    return Conversation(
        seed=seed.handle, nodes=result.nodes, occurrences=result.occurrences,
        incomplete=result.incomplete, missing_message_ids=result.missing, method=result.method,
        heuristic=result.method != "headers", searched_mailboxes=scope,
        truncated=gatherer.truncated)


__all__ = [
    "Conversation", "ConversationNode", "HeaderInfo", "Occurrence", "ThreadResult",
    "build_threads", "get_conversation", "normalize_id", "normalize_subject",
]
