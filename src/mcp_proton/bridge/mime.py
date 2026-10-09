"""MIME parsing and construction on the standard-library ``email`` package.

Contracts:

* **Parsing never raises on server-supplied bytes.** Malformed MIME degrades
  (a multipart without usable structure is read as ``text/plain``; unknown
  charsets decode with replacement). ``parse_message_with_notes`` additionally
  returns the defects it noticed as short, content-free notes.
* **HTML is never returned raw.** ``sanitize_html`` strips scripts, styles,
  forms, event handlers and *every remote resource reference*; only ``cid:``
  images survive. Remote assets are never fetched by this module.
* **Part ids** are IMAP-style paths: a single-part message is part ``"1"``; the
  children of a multipart root are ``"1"``, ``"2"``...; children of a nested
  multipart ``"2"`` are ``"2.1"``, ``"2.2"``. ``message/rfc822`` parts are leaves
  (listed as attachments, never descended into).
* **Building never emits ``Bcc``** unless ``include_bcc_header=True`` (drafts
  only, so the recipients survive a round trip). CR/LF in any caller-supplied
  header value raises ``MailError(INVALID_REQUEST)``.
* The reply/forward helpers are pure functions of an already parsed ``Message``.
"""

from __future__ import annotations

import contextlib
import email
import email.policy
import hashlib
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email import utils as email_utils
from email.headerregistry import Address as HdrAddress
from email.message import EmailMessage
from email.message import Message as PyMessage
from html.parser import HTMLParser
from typing import Any, Literal

import nh3

from ..domain.errors import ErrorCode, MailError, invalid
from ..domain.models import (
    Address,
    AttachmentInfo,
    Message,
    MessageBody,
    MessageHandle,
    OutgoingMessage,
)
from .ports import FetchedMessage

_POLICY = email.policy.default
_MSGID_RE = re.compile(r"<[^<>\s]+>")
_PART_ID_RE = re.compile(r"^\d+(\.\d+)*$")
MAX_REFERENCES = 20

# ------------------------------------------------------------------ sanitizing

_ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "center", "code", "col", "colgroup",
    "dd", "del", "div", "dl", "dt", "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i",
    "img", "ins", "li", "ol", "p", "pre", "q", "s", "small", "span", "strike", "strong",
    "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
}  # fmt: skip
_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"colspan", "rowspan", "align"},
    "th": {"colspan", "rowspan", "align"},
    "col": {"span"},
    "colgroup": {"span"},
    "ol": {"start", "type"},
    "*": {"dir", "lang"},
}
# Elements whose *content* is dropped too (not just the tag).
_DROP_CONTENT = {
    "script", "style", "iframe", "object", "embed", "noscript", "template", "title", "head",
    "svg", "math", "form", "select", "textarea", "button",
}  # fmt: skip
_LINK_SCHEMES = ("http:", "https:", "mailto:")
_IMG_NO_SRC_RE = re.compile(r"<img(?![^>]*\ssrc=)[^>]*>", re.IGNORECASE)


def _attr_filter(tag: str, attr: str, value: str) -> str | None:
    v = value.strip().lower()
    if tag == "img" and attr == "src":
        return value.strip() if v.startswith("cid:") else None  # drop remote and data:
    if tag == "a" and attr == "href":
        return value.strip() if v.startswith(_LINK_SCHEMES) else None
    return value


def sanitize_html(html: str) -> str:
    """Return a conservative, passive HTML fragment safe to display.

    Removes scripts/styles/forms/iframes, all event handlers and inline CSS, and
    every remote resource reference. ``cid:`` images are kept; ``data:`` images
    and remote images are dropped (an image left without a source is removed).
    Links stay but get ``rel="noopener noreferrer nofollow"``.
    """
    if not html:
        return ""
    cleaned = nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        clean_content_tags=_DROP_CONTENT,
        attributes=_ALLOWED_ATTRS,
        attribute_filter=_attr_filter,
        strip_comments=True,
        link_rel="noopener noreferrer nofollow",
        url_schemes={"http", "https", "mailto", "cid"},
    )
    return _IMG_NO_SRC_RE.sub("", cleaned)


class _TextExtractor(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "blockquote", "pre", "table", "hr"}  # fmt: skip
    _SKIP = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Crude tag stripper used only when a message has no text/plain part."""
    p = _TextExtractor()
    with contextlib.suppress(Exception):  # garbage input: keep whatever was extracted
        p.feed(html)
        p.close()
    text = "".join(p.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ------------------------------------------------------------------ part walking


def _is_container(part: PyMessage) -> bool:
    return part.get_content_maintype() == "multipart" and part.is_multipart()


def _leaves(msg: PyMessage) -> list[tuple[str, PyMessage]]:
    """All leaf parts with IMAP-style ids. A broken multipart (no children) is a leaf."""
    out: list[tuple[str, PyMessage]] = []

    def walk(part: PyMessage, prefix: str) -> None:
        for i, child in enumerate(part.get_payload(), start=1):  # type: ignore[arg-type]
            pid = f"{prefix}.{i}" if prefix else str(i)
            if _is_container(child):
                walk(child, pid)
            else:
                out.append((pid, child))

    if _is_container(msg):
        walk(msg, "")
    else:
        out.append(("1", msg))
    return out


def _disposition(part: PyMessage) -> str | None:
    try:
        d = part.get_content_disposition()
    except Exception:  # noqa: BLE001
        return None
    return d.lower() if d else None


def _filename(part: PyMessage) -> str | None:
    try:
        name = part.get_filename()
    except Exception:  # noqa: BLE001
        return None
    return str(name) if name else None


def _content_id(part: PyMessage) -> str | None:
    try:
        raw = part.get("Content-ID")
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    cid = str(raw).strip().strip("<>").strip()
    return cid or None


def _is_attachment(part: PyMessage) -> bool:
    ctype = part.get_content_type()
    if ctype.startswith("message/") or _disposition(part) == "attachment" or _filename(part):
        return True
    if part.get_content_maintype() == "multipart":  # malformed leaf, read as text
        return False
    return ctype not in ("text/plain", "text/html")


def _part_bytes(part: PyMessage) -> bytes:
    """Decoded content of a leaf part ("" when undecodable)."""
    try:
        if part.get_content_maintype() == "message":
            inner = part.get_payload()
            if isinstance(inner, list) and inner:
                return inner[0].as_bytes(  # type: ignore[union-attr]
                    policy=_POLICY.clone(refold_source="none")
                )
            return b""
        payload = part.get_payload(decode=True)
        return payload if isinstance(payload, bytes) else b""
    except Exception:  # noqa: BLE001
        return b""


def _part_text(part: PyMessage) -> str:
    data = _part_bytes(part)
    charset = part.get_content_charset() or "utf-8"
    try:
        return data.decode(charset, errors="replace")
    except (LookupError, ValueError):
        return data.decode("utf-8", errors="replace")


def _info(pid: str, part: PyMessage) -> AttachmentInfo:
    cid = _content_id(part)
    disp = _disposition(part)
    ctype = part.get_content_type()
    if part.get_content_maintype() == "multipart":
        ctype = "text/plain"
    return AttachmentInfo(
        part_id=pid,
        filename=_filename(part),
        content_type=ctype,
        size=len(_part_bytes(part)),
        content_id=cid,
        inline=disp == "inline" or (disp != "attachment" and cid is not None),
    )


def list_attachments(msg: EmailMessage) -> list[AttachmentInfo]:
    """Attachments (incl. inline CID parts and nested ``message/rfc822``)."""
    return [_info(pid, p) for pid, p in _leaves(msg) if _is_attachment(p)]


def get_part(raw: bytes, part_id: str) -> tuple[AttachmentInfo, bytes]:
    """Decoded bytes of one leaf part. Raises ``MailError(NOT_FOUND)``."""
    if not _PART_ID_RE.match(part_id or ""):
        raise MailError(ErrorCode.NOT_FOUND, "no such message part")
    msg = _parse(raw)
    for pid, part in _leaves(msg):
        if pid == part_id:
            return _info(pid, part), _part_bytes(part)
    raise MailError(ErrorCode.NOT_FOUND, "no such message part")


def _parse(raw: bytes) -> EmailMessage:
    try:
        parsed = email.message_from_bytes(raw, policy=_POLICY)
    except Exception:  # noqa: BLE001 - last-resort degrade
        parsed = email.message_from_bytes(b"\r\n" + raw, policy=_POLICY)
    return parsed  # type: ignore[return-value]


def _defect_notes(msg: PyMessage) -> list[str]:
    names: list[str] = []
    try:
        for part in msg.walk():
            for d in part.defects:
                n = type(d).__name__
                if n not in names:
                    names.append(n)
    except Exception:  # noqa: BLE001
        names.append("WalkFailed")
    return [f"mime defect: {n}" for n in names[:10]]


def _bodies(msg: PyMessage) -> tuple[str | None, str | None]:
    texts: list[str] = []
    htmls: list[str] = []
    for _pid, part in _leaves(msg):
        if _is_attachment(part):
            continue
        if part.get_content_type() == "text/html":
            htmls.append(_part_text(part))
        else:
            texts.append(_part_text(part))
    return ("\n\n".join(texts) if texts else None, "\n\n".join(htmls) if htmls else None)


# ------------------------------------------------------------------ header access


def _hdr(msg: PyMessage, name: str) -> str | None:
    try:
        v = msg.get(name)
    except Exception:  # noqa: BLE001
        return None
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _pairs(v: Any) -> list[tuple[str, str]]:
    try:
        if hasattr(v, "addresses"):
            return [(a.display_name, a.addr_spec) for a in v.addresses]
        return email_utils.getaddresses([str(v)])
    except Exception:  # noqa: BLE001
        return []


def _addresses(msg: PyMessage, name: str) -> list[Address]:
    out: list[Address] = []
    try:
        values = msg.get_all(name) or []
    except Exception:  # noqa: BLE001
        return out
    for v in values:
        for display, addr in _pairs(v):
            try:
                out.append(Address(name=display or None, email=addr))
            except ValueError:
                continue  # skip unparseable entries
    return out


def _date(msg: PyMessage) -> datetime | None:
    raw = _hdr(msg, "Date")
    if not raw:
        return None
    try:
        return email_utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None


def _msgid_list(value: str | None) -> list[str]:
    return _MSGID_RE.findall(value) if value else []


def _in_reply_to(msg: PyMessage) -> str | None:
    ids = _msgid_list(_hdr(msg, "In-Reply-To"))
    return ids[-1] if ids else None


def _references(msg: PyMessage) -> list[str]:
    try:
        values = [str(v) for v in msg.get_all("References") or []]
    except Exception:  # noqa: BLE001
        return []
    return _msgid_list(" ".join(values))


def _all_headers(msg: PyMessage) -> dict[str, list[str]]:
    headers: dict[str, list[str]] = {}
    try:
        items = list(msg.raw_items())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return headers
    for name, value in items:
        try:
            text = str(msg.policy.header_fetch_parse(name, value))
        except Exception:  # noqa: BLE001
            text = str(value)
        headers.setdefault(name, []).append(text)
    return headers


# ------------------------------------------------------------------ parse_message


def parse_message_with_notes(
    fetched: FetchedMessage,
    handle: MessageHandle,
    *,
    include_headers: bool,
    include_body: bool,
    max_body_chars: int,
    body_format: Literal["text", "html", "both"] = "both",
) -> tuple[Message, list[str]]:
    """Like :func:`parse_message`, plus content-free notes about MIME defects."""
    msg = _parse(fetched.raw)
    notes = _defect_notes(msg)

    attachments: list[AttachmentInfo] = []
    body: MessageBody | None = None
    if not fetched.header_only:
        attachments = list_attachments(msg)
        if include_body:
            body = _build_body(msg, max_body_chars, body_format)

    message = Message(
        handle=handle.token(),
        mailbox=fetched.mailbox,
        uid=fetched.uid,
        message_id=_hdr(msg, "Message-ID"),
        subject=_hdr(msg, "Subject"),
        from_=_addresses(msg, "From"),  # type: ignore[call-arg]  # alias populate_by_name
        to=_addresses(msg, "To"),
        cc=_addresses(msg, "Cc"),
        bcc=_addresses(msg, "Bcc"),
        reply_to=_addresses(msg, "Reply-To"),
        date=_date(msg),
        internal_date=fetched.internal_date,
        size=fetched.size,
        flags=list(fetched.flags),
        has_attachments=None if fetched.header_only else bool(attachments),
        in_reply_to=_in_reply_to(msg),
        references=_references(msg),
        headers=_all_headers(msg) if include_headers else None,
        body=body,
        attachments=attachments,
    )
    return message, notes


def parse_message(
    fetched: FetchedMessage,
    handle: MessageHandle,
    *,
    include_headers: bool,
    include_body: bool,
    max_body_chars: int,
    body_format: Literal["text", "html", "both"] = "both",
) -> Message:
    """Parse raw bytes into the domain ``Message``. Never raises on bad MIME."""
    return parse_message_with_notes(
        fetched,
        handle,
        include_headers=include_headers,
        include_body=include_body,
        max_body_chars=max_body_chars,
        body_format=body_format,
    )[0]


def _build_body(
    msg: PyMessage, max_chars: int, body_format: Literal["text", "html", "both"]
) -> MessageBody:
    text, html = _bodies(msg)
    truncated = False
    if html is not None and len(html) > max_chars:
        html, truncated = html[:max_chars], True
    if text is not None and len(text) > max_chars:
        text, truncated = text[:max_chars], True
    if text is None and html is not None:
        text = html_to_text(html)
        if len(text) > max_chars:
            text, truncated = text[:max_chars], True
    out_text = text if body_format in ("text", "both") else None
    out_html = sanitize_html(html) if html is not None and body_format in ("html", "both") else None
    return MessageBody(text=out_text, html=out_html, html_sanitized=True, truncated=truncated)


# ------------------------------------------------------------------ building


@dataclass
class ResolvedAttachment:
    filename: str
    content_type: str
    data: bytes
    inline_cid: str | None = None


def make_message_id(domain: str) -> str:
    """``<random@domain>``; one per outgoing operation, preserved across retries."""
    _no_crlf("domain", domain)
    return f"<{secrets.token_urlsafe(18)}@{domain.strip() or 'localhost'}>"


def _no_crlf(what: str, value: str | None) -> None:
    if value and any(c in value for c in "\r\n\x00"):
        raise invalid(f"{what} contains line breaks")


def _hdr_addr(a: Address) -> HdrAddress:
    _no_crlf("address name", a.name)
    _no_crlf("address", a.email)
    try:
        return HdrAddress(display_name=a.name or "", addr_spec=a.email)
    except Exception as exc:  # noqa: BLE001
        raise invalid("invalid address") from exc


def _check_msgid(what: str, value: str) -> str:
    v = value.strip()
    if not re.fullmatch(r"<[^<>\s]+>", v):
        raise invalid(f"{what} must be a bracketed Message-ID")
    return v


def build_message(
    out: OutgoingMessage,
    *,
    from_addr: Address,
    message_id: str,
    date: datetime | None = None,
    attachments: Sequence[ResolvedAttachment] = (),
    forwarded_raw: bytes | None = None,
    include_bcc_header: bool = False,
) -> bytes:
    """Serialize an outgoing message (CRLF line endings, 7-bit safe).

    ``Bcc`` is envelope-only and omitted unless ``include_bcc_header`` (drafts).
    Raises ``MailError(INVALID_REQUEST)`` on header injection.
    """
    _no_crlf("subject", out.subject)
    msg = EmailMessage(policy=email.policy.SMTP)
    msg["MIME-Version"] = "1.0"
    msg["From"] = _hdr_addr(from_addr)
    for name, addrs in (("To", out.to), ("Cc", out.cc), ("Reply-To", out.reply_to)):
        if addrs:
            msg[name] = [_hdr_addr(a) for a in addrs]
    if include_bcc_header and out.bcc:
        msg["Bcc"] = [_hdr_addr(a) for a in out.bcc]
    msg["Subject"] = out.subject
    when = date or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    msg["Date"] = email_utils.format_datetime(when)
    msg["Message-ID"] = _check_msgid("message_id", message_id)
    if out.in_reply_to:
        msg["In-Reply-To"] = _check_msgid("in_reply_to", out.in_reply_to)
    if out.references:
        msg["References"] = " ".join(_check_msgid("references", r) for r in out.references)

    has_html = bool(out.html)
    if out.text and has_html:
        msg.set_content(out.text)
        msg.add_alternative(out.html, subtype="html")
    elif has_html:
        msg.set_content(out.html, subtype="html")
    else:
        msg.set_content(out.text or "")

    for att in attachments:
        _no_crlf("filename", att.filename)
        _no_crlf("content type", att.content_type)
        _no_crlf("content id", att.inline_cid)
    inline = [a for a in attachments if a.inline_cid and has_html]
    regular = [a for a in attachments if not (a.inline_cid and has_html)]
    if inline:
        html_part = msg.get_body(preferencelist=("html",))
        if html_part is None:  # pragma: no cover - has_html guarantees it
            raise invalid("cannot attach inline images without an html body")
        for a in inline:
            maintype, subtype = _split_type(a.content_type)
            cid = (a.inline_cid or "").strip("<>")
            html_part.add_related(  # type: ignore[attr-defined]
                a.data,
                maintype,
                subtype,
                cid=f"<{cid}>",
                filename=a.filename,
                disposition="inline",
            )
    for a in regular:
        maintype, subtype = _split_type(a.content_type)
        msg.add_attachment(a.data, maintype=maintype, subtype=subtype, filename=a.filename)
    if forwarded_raw is not None:
        inner = email.message_from_bytes(forwarded_raw, policy=email.policy.compat32)
        msg.add_attachment(inner, disposition="attachment", filename="forwarded message.eml")

    try:
        return msg.as_bytes(policy=email.policy.SMTP)
    except Exception as exc:  # noqa: BLE001
        raise invalid("message could not be serialized") from exc


def _split_type(content_type: str) -> tuple[str, str]:
    m = re.fullmatch(r"([A-Za-z0-9!#$&^_.+-]+)/([A-Za-z0-9!#$&^_.+-]+)", content_type.strip())
    return (m.group(1).lower(), m.group(2).lower()) if m else ("application", "octet-stream")


# ------------------------------------------------------------------ drafts


def extract_draft_payload(raw: bytes) -> dict[str, Any]:
    """Convert a stored draft into ``OutgoingMessage`` fields (sans ``account``).

    Keys: ``from`` (or None), ``to``, ``cc``, ``bcc``, ``reply_to`` (lists of
    ``{"name","email"}``), ``subject``, ``text``, ``html`` (unsanitized, as the
    author wrote it), ``in_reply_to``, ``references``. Attachments are not
    included; use :func:`list_attachments` / :func:`get_part`.
    """
    msg = _parse(raw)
    text, html = _bodies(msg)

    def dump(name: str) -> list[dict[str, Any]]:
        return [a.model_dump() for a in _addresses(msg, name)]

    senders = dump("From")
    return {
        "from": senders[0] if senders else None,
        "to": dump("To"),
        "cc": dump("Cc"),
        "bcc": dump("Bcc"),
        "reply_to": dump("Reply-To"),
        "subject": _hdr(msg, "Subject") or "",
        "text": text,
        "html": html,
        "in_reply_to": _in_reply_to(msg),
        "references": _references(msg),
    }


REVIEW_TEXT_CHARS = 20_000
MAX_REVIEW_ATTACHMENTS = 50


def review_block(raw: bytes, limit: int = REVIEW_TEXT_CHARS) -> dict[str, Any]:
    """What an approver must see for the exact bytes that will be sent: headers, the
    (bounded) text and HTML-as-text, and each attachment's name, type, size and digest."""
    msg = _parse(raw)
    text, html = _bodies(msg)
    atts = [
        {"filename": i.filename, "content_type": i.content_type, "size": i.size,
         "sha256": hashlib.sha256(_part_bytes(p)).hexdigest()}
        for i, p in ((_info(pid, p), p) for pid, p in _leaves(msg) if _is_attachment(p))
    ][:MAX_REVIEW_ATTACHMENTS]
    date = _date(msg)
    return {
        "from": [a.formatted() for a in _addresses(msg, "From")],
        "to": [a.formatted() for a in _addresses(msg, "To")],
        "subject": _hdr(msg, "Subject") or "",
        "date": date.isoformat() if date else None,
        "text": (text or "")[:limit],
        "text_truncated": len(text or "") > limit,
        "html_text": html_to_text(html)[:limit] if html else "",
        "attachments": atts,
    }


# ------------------------------------------------------------------ reply / forward

_RE_PREFIX = re.compile(r"^\s*re\s*:", re.IGNORECASE)
_FWD_PREFIX = re.compile(r"^\s*fw[d]?\s*:", re.IGNORECASE)


def reply_headers(original: Message) -> tuple[str | None, list[str]]:
    """``(In-Reply-To, References)``: original references + its Message-ID, deduped,
    bounded to the last ``MAX_REFERENCES``."""
    refs = list(original.references)
    if original.message_id:
        refs.append(original.message_id)
    seen: set[str] = set()
    unique: list[str] = []
    for r in reversed(refs):  # keep the most recent occurrence
        if r not in seen:
            seen.add(r)
            unique.append(r)
    unique.reverse()
    return original.message_id, unique[-MAX_REFERENCES:]


def reply_subject(subject: str | None) -> str:
    s = (subject or "").strip()
    return s if _RE_PREFIX.match(s) else f"Re: {s}".rstrip() if s else "Re: "


def forward_subject(subject: str | None) -> str:
    s = (subject or "").strip()
    return s if _FWD_PREFIX.match(s) else f"Fwd: {s}".rstrip() if s else "Fwd: "


def reply_recipients(
    original: Message, self_addresses: set[str], reply_all: bool
) -> tuple[list[Address], list[Address]]:
    """``(to, cc)`` for a reply. Self addresses are excluded and results are
    deduplicated case-insensitively. A message you sent replies to its own To."""
    selfs = {a.strip().lower() for a in self_addresses}
    sent_by_self = any(a.email.lower() in selfs for a in original.from_)
    primary = list(original.to) if sent_by_self else (original.reply_to or original.from_)
    others = [*original.to, *original.cc] if reply_all else []
    if sent_by_self and reply_all:
        others = list(original.cc)

    seen: set[str] = set()

    def take(addrs: list[Address], allow_self: bool = False) -> list[Address]:
        out: list[Address] = []
        for a in addrs:
            key = a.email.lower()
            if key in seen or (key in selfs and not allow_self):
                continue
            seen.add(key)
            out.append(a)
        return out

    to = take(primary)
    if not to:  # e.g. replying to a note-to-self
        to = take(primary, allow_self=True)
    cc = take(others)
    return to, cc


def _who(a: Address) -> str:
    return a.name or a.email


def _body_text(original: Message) -> str:
    if original.body is None:
        return ""
    if original.body.text:
        return original.body.text
    return html_to_text(original.body.html) if original.body.html else ""


def quote_text(original: Message) -> str:
    """``On <date>, <name> wrote:`` followed by the body quoted with ``> ``."""
    sender = _who(original.from_[0]) if original.from_ else "the sender"
    when = original.date.strftime("%a, %b %d, %Y at %H:%M") if original.date else "an earlier date"
    quoted = "\n".join(f"> {ln}" if ln else ">" for ln in _body_text(original).splitlines())
    return f"On {when}, {sender} wrote:\n{quoted}"


def forward_inline_text(original: Message) -> str:
    """Header block + body for an inline forward."""

    def fmt(addrs: list[Address]) -> str:
        return ", ".join(a.formatted() for a in addrs)

    lines = ["---------- Forwarded message ----------"]
    if original.from_:
        lines.append(f"From: {fmt(original.from_)}")
    if original.date:
        lines.append(f"Date: {original.date.strftime('%a, %b %d, %Y at %H:%M')}")
    lines.append(f"Subject: {original.subject or ''}")
    if original.to:
        lines.append(f"To: {fmt(original.to)}")
    if original.cc:
        lines.append(f"Cc: {fmt(original.cc)}")
    return "\n".join(lines) + "\n\n" + _body_text(original)
