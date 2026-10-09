"""Adapter contracts the application service depends on.

``bridge.imap.ImapMailStore`` and ``bridge.smtp.SmtpTransport`` implement these.
Services must only use these methods so adapters stay swappable and testable.

Conventions:

* Every message-addressing call takes ``(mailbox, uidvalidity, uids)``. The
  adapter SELECTs the mailbox and raises ``MailError(STALE_HANDLE)`` if the
  server's UIDVALIDITY differs. UIDs only; never sequence numbers.
* Reads use BODY.PEEK so they never set \\Seen implicitly.
* Methods are synchronous and thread-safe (adapter serializes per connection).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from ..domain.models import (
    Address,
    MailboxInfo,
    MailboxRole,
    MessageSummary,
    RecipientResult,
    SearchQuery,
)


@dataclass
class CapabilityReport:
    account: str
    server_capabilities: list[str]
    # name -> "available" | "unavailable" | "unverified"
    features: dict[str, str] = field(default_factory=dict)
    special_folders: dict[str, str] = field(default_factory=dict)  # role -> mailbox
    delimiter: str | None = None
    server_id: dict[str, str] | None = None  # IMAP ID response (Bridge version when exposed)
    checked_at: datetime | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class FetchedMessage:
    """Raw material for a single message; MIME parsing happens in bridge.mime."""

    mailbox: str
    uidvalidity: int
    uid: int
    flags: list[str]
    internal_date: datetime | None
    size: int | None
    raw: bytes  # full RFC822 or header-only bytes, depending on request
    header_only: bool = False


@dataclass
class AppendResult:
    mailbox: str
    uidvalidity: int | None
    uid: int | None  # None when the server lacks UIDPLUS APPENDUID and lookup failed


@dataclass
class CopyResult:
    # source uid -> destination uid (None when the server did not report it)
    mapping: dict[int, int | None]
    dest_uidvalidity: int | None


class MailStore(Protocol):
    account: str

    # discovery
    def capabilities(self, refresh: bool = False) -> CapabilityReport: ...
    def has_capability(self, name: str) -> bool: ...
    def list_mailboxes(self, with_counts: bool = False) -> list[MailboxInfo]: ...
    def mailbox_status(self, mailbox: str) -> MailboxInfo: ...
    def role_of(self, mailbox: str) -> MailboxRole: ...
    def mailbox_for_role(self, role: MailboxRole) -> str | None: ...

    # reading
    def list_uids(self, mailbox: str, criteria: list[object] | None = None
                  ) -> tuple[int, list[int]]: ...
    def search(self, mailbox: str, query: SearchQuery) -> tuple[int, list[int], list[str]]: ...
    def fetch_summaries(self, mailbox: str, uidvalidity: int, uids: list[int]
                        ) -> list[MessageSummary]: ...
    def fetch_message(self, mailbox: str, uidvalidity: int, uid: int,
                      header_only: bool = False) -> FetchedMessage: ...
    def fetch_flags(self, mailbox: str, uidvalidity: int, uids: list[int]
                    ) -> dict[int, list[str]]: ...
    def find_by_message_id(self, mailbox: str, message_id: str) -> tuple[int, list[int]]: ...

    # mutation
    def store_flags(self, mailbox: str, uidvalidity: int, uids: list[int],
                    add: list[str] | None = None, remove: list[str] | None = None
                    ) -> dict[int, list[str]]: ...
    def copy(self, mailbox: str, uidvalidity: int, uids: list[int], dest: str) -> CopyResult: ...
    def move(self, mailbox: str, uidvalidity: int, uids: list[int], dest: str) -> CopyResult: ...
    def append(self, mailbox: str, raw: bytes, flags: list[str] | None = None,
               internal_date: datetime | None = None) -> AppendResult: ...
    def expunge_uids(self, mailbox: str, uidvalidity: int, uids: list[int]) -> list[int]: ...
    def create_mailbox(self, name: str) -> None: ...
    def rename_mailbox(self, old: str, new: str) -> None: ...
    def delete_mailbox(self, name: str) -> None: ...
    def set_subscribed(self, name: str, subscribed: bool) -> None: ...

    # change tracking
    def idle_wait(self, mailbox: str, timeout: float) -> list[tuple[object, ...]]: ...

    def close(self) -> None: ...


class SmtpSendError(Exception):
    """Raised when SMTP fails *before* any recipient was accepted (safe to retry)."""


class SmtpDeliveryUnknown(Exception):  # noqa: N818 - domain name, not an error class
    """Raised when the connection failed after DATA may have been accepted."""

    def __init__(self, message: str, recipients: list[RecipientResult]) -> None:
        super().__init__(message)
        self.recipients = recipients


class MailTransport(Protocol):
    account: str

    def send(self, envelope_from: str, recipients: list[str], raw: bytes
             ) -> list[RecipientResult]:
        """Submit one message. Returns per-recipient acceptance. Raises
        ``SmtpSendError`` if nothing was submitted, ``SmtpDeliveryUnknown`` if the
        outcome is ambiguous. Never retries already-accepted recipients."""
        ...

    def check(self) -> None: ...


@dataclass
class SenderIdentity:
    address: Address
    primary: bool = False
