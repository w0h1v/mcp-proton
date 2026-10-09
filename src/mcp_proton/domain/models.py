"""Typed request/result models shared by MCP, CLI, UI and jobs.

Identity rules (design: "Reliability and data handling"):

* A ``MessageHandle`` identifies one *mailbox occurrence*: account, mailbox,
  UIDVALIDITY and UID. Message-ID is never used as a mutation locator and
  sequence numbers are never stored.
* Handles are serialized as opaque tokens so resource URIs and tool payloads
  never contain subjects, addresses or mailbox names in clear text.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .errors import invalid

_HANDLE_PREFIX = "h1."
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class MessageHandle(Model):
    model_config = ConfigDict(frozen=True, extra="forbid")

    account: str
    mailbox: str
    uidvalidity: int = Field(ge=1)
    uid: int = Field(ge=1)

    def token(self) -> str:
        raw = json.dumps(
            [self.account, self.mailbox, self.uidvalidity, self.uid],
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return _HANDLE_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @classmethod
    def parse(cls, token: str) -> MessageHandle:
        if not isinstance(token, str) or not token.startswith(_HANDLE_PREFIX):
            raise invalid("malformed message handle")
        body = token[len(_HANDLE_PREFIX) :]
        try:
            raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
            account, mailbox, uidvalidity, uid = json.loads(raw)
            return cls(account=account, mailbox=mailbox, uidvalidity=uidvalidity, uid=uid)
        except Exception as exc:  # noqa: BLE001 - any decode failure is the same client error
            raise invalid("malformed message handle") from exc


class MailboxRole(StrEnum):
    INBOX = "inbox"
    SENT = "sent"
    DRAFTS = "drafts"
    TRASH = "trash"
    SPAM = "spam"
    ARCHIVE = "archive"
    ALL_MAIL = "all_mail"
    STARRED = "starred"
    SCHEDULED = "scheduled"
    FOLDER = "folder"  # user folder (exclusive location)
    LABEL = "label"  # Proton label exposed as a mailbox (non-exclusive membership)
    CONTAINER = "container"  # \Noselect parent such as "Folders" or "Labels"
    OTHER = "other"


class MailboxInfo(Model):
    account: str
    name: str
    delimiter: str | None = None
    attributes: list[str] = Field(default_factory=list)
    role: MailboxRole = MailboxRole.OTHER
    selectable: bool = True
    subscribed: bool | None = None
    messages: int | None = None
    unseen: int | None = None
    uidvalidity: int | None = None
    uidnext: int | None = None
    permanent_flags: list[str] | None = None
    writable: bool | None = None


class Address(Model):
    name: str | None = None
    email: str

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        v = v.strip()
        if "@" not in v or any(c in v for c in "\r\n<>, "):
            raise ValueError("invalid email address")
        return v

    def formatted(self) -> str:
        from email.utils import formataddr

        return formataddr((self.name or "", self.email))


class AttachmentInfo(Model):
    part_id: str  # MIME part path, e.g. "2" or "1.2"
    filename: str | None = None
    content_type: str
    size: int | None = None
    content_id: str | None = None
    inline: bool = False


class MessageSummary(Model):
    handle: str  # opaque MessageHandle token
    mailbox: str
    uid: int
    message_id: str | None = None
    subject: str | None = None
    from_: list[Address] = Field(default_factory=list, alias="from")
    to: list[Address] = Field(default_factory=list)
    cc: list[Address] = Field(default_factory=list)
    date: datetime | None = None
    internal_date: datetime | None = None
    size: int | None = None
    flags: list[str] = Field(default_factory=list)
    has_attachments: bool | None = None


class MessageBody(Model):
    text: str | None = None
    html: str | None = None  # sanitized; remote assets never fetched
    html_sanitized: bool = True
    truncated: bool = False


class Message(MessageSummary):
    bcc: list[Address] = Field(default_factory=list)
    reply_to: list[Address] = Field(default_factory=list)
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    headers: dict[str, list[str]] | None = None
    body: MessageBody | None = None
    attachments: list[AttachmentInfo] = Field(default_factory=list)


class Page(Model):
    items: list[Any]
    total: int | None = None
    next_cursor: str | None = None


# ---------------------------------------------------------------- search


class SearchQuery(Model):
    """Structured query translated to IMAP SEARCH. Leaves are AND-ed unless
    combined with ``any_of`` (OR) or ``not_`` (NOT)."""

    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    cc: str | None = None
    bcc: str | None = None
    subject: str | None = None
    body: str | None = None
    text: str | None = None
    since: datetime | None = None
    before: datetime | None = None
    larger_than: int | None = None
    smaller_than: int | None = None
    seen: bool | None = None
    flagged: bool | None = None
    answered: bool | None = None
    draft: bool | None = None
    deleted: bool | None = None
    keyword: str | None = None
    header: dict[str, str] | None = None
    uid_range: str | None = None
    any_of: list[SearchQuery] | None = Field(default=None, max_length=20)
    not_: SearchQuery | None = Field(default=None, alias="not")

    @field_validator("from_", "to", "cc", "bcc", "subject", "body", "text", "keyword",
                     "uid_range")
    @classmethod
    def _no_control_chars(cls, v: str | None) -> str | None:
        if v is not None and _CONTROL_CHARS.search(v):
            raise ValueError("must not contain control characters")
        return v

    @field_validator("header")
    @classmethod
    def _header_no_control_chars(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        for k, val in (v or {}).items():
            if _CONTROL_CHARS.search(k) or _CONTROL_CHARS.search(val) or ":" in k:
                raise ValueError("header names/values must not contain control characters")
        return v


class SearchResult(Model):
    items: list[MessageSummary]
    total: int
    next_cursor: str | None = None
    scope: list[str]  # mailboxes searched
    completeness: Literal["complete", "unknown"] = "unknown"
    notes: list[str] = Field(default_factory=list)  # disclosed fallbacks


# ---------------------------------------------------------------- outgoing


class LocalAttachment(Model):
    """A local file to attach; must be inside an owner-configured ingest dir."""

    path: str
    filename: str | None = None
    content_type: str | None = None
    inline_cid: str | None = None


class ArtifactAttachment(Model):
    """A managed artifact handle (previously fetched attachment)."""

    artifact_id: str
    filename: str | None = None
    inline_cid: str | None = None


class OutgoingMessage(Model):
    account: str
    from_: Address | None = Field(default=None, alias="from")  # default: account address
    to: list[Address] = Field(default_factory=list)
    cc: list[Address] = Field(default_factory=list)
    bcc: list[Address] = Field(default_factory=list)
    reply_to: list[Address] = Field(default_factory=list)
    subject: str = ""
    text: str | None = None
    html: str | None = None
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    attachments: list[LocalAttachment | ArtifactAttachment] = Field(default_factory=list)
    forward_as_attachment: str | None = None  # message handle token

    def all_recipients(self) -> list[str]:
        return [a.email for a in (*self.to, *self.cc, *self.bcc)]


class RecipientResult(Model):
    email: str
    accepted: bool
    code: int | None = None
    message: str | None = None


class SendResult(Model):
    message_id: str
    status: Literal["accepted", "partially_accepted", "rejected", "delivery_unknown"]
    recipients: list[RecipientResult] = Field(default_factory=list)
    sent_copy: str | None = None  # handle token, when reconciled in Sent


# ---------------------------------------------------------------- operations


class OperationStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    PARTIALLY_SUCCEEDED = "partially_succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DELIVERY_UNKNOWN = "delivery_unknown"


TERMINAL_STATUSES = frozenset(
    {
        OperationStatus.DENIED,
        OperationStatus.EXPIRED,
        OperationStatus.SUCCEEDED,
        OperationStatus.PARTIALLY_SUCCEEDED,
        OperationStatus.FAILED,
        OperationStatus.CANCELLED,
        OperationStatus.DELIVERY_UNKNOWN,
    }
)


class ItemResult(Model):
    target: str
    status: Literal["succeeded", "failed", "skipped", "pending"]
    detail: str | None = None
    new_handle: str | None = None


class OperationOutcome(Model):
    """Uniform machine-readable result of any write."""

    status: OperationStatus
    operation_id: str
    kind: str
    summary: str
    expires_at: datetime | None = None
    result: dict[str, Any] | None = None
    items: list[ItemResult] | None = None
    error: dict[str, Any] | None = None
