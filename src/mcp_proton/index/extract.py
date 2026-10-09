"""Attachment text extraction for the local index.

Only formats readable with the standard library are supported:
``text/plain``, ``text/html``, ``text/csv`` and ``application/json``.

PDF, Office and other binary formats are deliberately *not* handled here: they
need third-party parsers (and a decision about running them on untrusted
input). That would be an optional plugin registered through
:func:`register_extractor`; none ships with mcp-proton.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from ..bridge.mime import html_to_text

MAX_EXTRACT_BYTES = 2 * 1024 * 1024
MAX_EXTRACT_CHARS = 200_000

Extractor = Callable[[bytes, str | None], str]


def _decode(data: bytes, charset: str | None) -> str:
    try:
        return data.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _plain(data: bytes, charset: str | None) -> str:
    return _decode(data, charset)


def _html(data: bytes, charset: str | None) -> str:
    return html_to_text(_decode(data, charset))


def _json(data: bytes, charset: str | None) -> str:
    text = _decode(data, charset)
    try:
        value = json.loads(text)
    except ValueError:
        return text
    parts: list[str] = []

    def walk(v: object) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                parts.append(str(k))
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif v is not None:
            parts.append(str(v))

    walk(value)
    return "\n".join(parts)


_EXTRACTORS: dict[str, Extractor] = {
    "text/plain": _plain,
    "text/csv": _plain,
    "text/html": _html,
    "application/json": _json,
}


def register_extractor(content_type: str, fn: Extractor) -> None:
    """Hook for an optional plugin (for example PDF text). Not used by default."""
    _EXTRACTORS[content_type.lower()] = fn


def supported(content_type: str) -> bool:
    return content_type.lower().split(";")[0].strip() in _EXTRACTORS


def extract_text(content_type: str, data: bytes, charset: str | None = None) -> str | None:
    """Text of an attachment, or ``None`` when the type is unsupported or too large."""
    fn = _EXTRACTORS.get(content_type.lower().split(";")[0].strip())
    if fn is None or len(data) > MAX_EXTRACT_BYTES:
        return None
    try:
        return fn(data, charset)[:MAX_EXTRACT_CHARS]
    except Exception:  # noqa: BLE001 - extraction must never break indexing
        return None
