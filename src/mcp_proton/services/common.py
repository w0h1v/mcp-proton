"""Helpers shared by service modules: handle parsing, grouping, paging, items."""

from __future__ import annotations

import base64
import json
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from ..domain.errors import ErrorCode, MailError, invalid
from ..domain.models import ItemResult, MessageHandle

MAX_BATCH = 1000


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
