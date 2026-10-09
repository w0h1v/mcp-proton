"""Helpers shared by service modules: handle parsing, grouping, paging, items."""

from __future__ import annotations

import base64
import json
import time
from collections import defaultdict
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from ..domain.errors import ErrorCode, MailError, invalid, not_found
from ..domain.models import ItemResult, MessageHandle, canonical_inbox

if TYPE_CHECKING:
    from .core import MailApp

MAX_BATCH = 1000


_MAILBOX_CACHE_TTL = 15.0


def canonical_mailbox(app: MailApp, account: str, name: str, *, must_exist: bool = True) -> str:
    """Canonical server spelling of a caller-supplied mailbox name.

    Every case variant of INBOX (case-insensitive per RFC 3501) maps to ``INBOX`` so
    policy rules and mailbox constraints cannot be dodged by changing case. Other
    names are case-sensitive and must exist in the server LIST (``NOT_FOUND``
    otherwise) unless ``must_exist`` is False (creation targets)."""
    if not isinstance(name, str) or not name:
        raise invalid("mailbox name is required")
    name = canonical_inbox(name)
    if name == "INBOX" or not must_exist:
        return name
    cache: dict[str, tuple[float, set[str]]] = app.__dict__.setdefault("_mailbox_cache", {})
    hit = cache.get(account)
    if hit is not None and time.monotonic() - hit[0] < _MAILBOX_CACHE_TTL and name in hit[1]:
        return name
    names = {m.name for m in app.store(account).list_mailboxes()}
    cache[account] = (time.monotonic(), names)
    if name not in names:
        raise not_found("mailbox does not exist")
    return name


def canonical_mailboxes(app: MailApp, account: str, names: Iterable[str], *,
                        must_exist: bool = True) -> list[str]:
    return list(dict.fromkeys(canonical_mailbox(app, account, n, must_exist=must_exist)
                              for n in names))


def review_text(payload: dict[str, Any], limit: int | None = None) -> str | None:
    """Plain-text rendering of the approver review block(s) stored in an operation
    payload (``review`` for draft sends, ``forward.review`` for forward-as-attachment).
    Callers that show HTML must escape the result."""
    blocks: list[tuple[str, Any]] = []
    if isinstance(payload.get("review"), dict):
        blocks.append(("Content to be sent", payload["review"]))
    fwd = payload.get("forward")
    if isinstance(fwd, dict) and isinstance(fwd.get("review"), dict):
        blocks.append(("Forwarded message (attached as message/rfc822)", fwd["review"]))
    if not blocks:
        return None
    out: list[str] = []
    for title, r in blocks:
        out.append(f"== {title} ==")
        for label, key in (("From", "from"), ("To", "to")):
            v = r.get(key)
            if v:
                out.append(f"{label}: " + (", ".join(map(str, v)) if isinstance(v, list)
                                           else str(v)))
        for label, key in (("Subject", "subject"), ("Date", "date")):
            if r.get(key):
                out.append(f"{label}: {r[key]}")
        if r.get("text"):
            out.append("Text:\n" + str(r["text"]) + ("\n[truncated]" if r.get("text_truncated")
                                                     else ""))
        if r.get("html_text"):
            out.append("HTML (as text):\n" + str(r["html_text"]))
        atts = r.get("attachments") or []
        out.append(f"Attachments ({len(atts)}):" if atts else "Attachments: none")
        for a in atts:
            out.append(f"  - {a.get('filename') or '(unnamed)'} [{a.get('content_type')}] "
                       f"{a.get('size')} bytes sha256={a.get('sha256')}")
    text = "\n".join(out)
    return text if limit is None or len(text) <= limit else text[:limit] + "\n[truncated]"


def parse_handles(tokens: Iterable[str], *, account: str | None = None,
                  max_items: int = MAX_BATCH) -> list[MessageHandle]:
    """Parse opaque handle tokens; all must belong to one account."""
    handles = [MessageHandle.parse(t) for t in tokens]
    if not handles:
        raise invalid("at least one message handle is required")
    if len(handles) > max_items:
        raise MailError(ErrorCode.LIMIT_EXCEEDED, f"at most {max_items} messages per request")
    accounts = {h.account for h in handles}
    if len(accounts) != 1 or (account is not None and accounts != {account}):
        raise invalid("all handles must belong to the same account")
    # de-duplicate while preserving order
    seen: set[MessageHandle] = set()
    out = []
    for h in handles:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def group_by_mailbox(handles: Iterable[MessageHandle]
                     ) -> dict[tuple[str, int], list[MessageHandle]]:
    groups: dict[tuple[str, int], list[MessageHandle]] = defaultdict(list)
    for h in handles:
        groups[(h.mailbox, h.uidvalidity)].append(h)
    return dict(groups)


def handles_payload(handles: Iterable[MessageHandle]) -> list[str]:
    return [h.token() for h in handles]


def encode_cursor(data: dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> dict[str, Any] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except Exception as exc:  # noqa: BLE001
        raise invalid("malformed cursor") from exc


def bulk_status(items: list[ItemResult]) -> str:
    """Overall operation status from per-item results (no false atomicity)."""
    from ..domain.models import OperationStatus

    ok = sum(i.status == "succeeded" for i in items)
    if items and ok == len(items):
        return OperationStatus.SUCCEEDED.value
    if ok == 0 and all(i.status in ("failed", "skipped") for i in items):
        return OperationStatus.FAILED.value
    return OperationStatus.PARTIALLY_SUCCEEDED.value
