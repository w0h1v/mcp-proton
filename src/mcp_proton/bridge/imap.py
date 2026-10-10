"""IMAP adapter for Proton Mail Bridge: ``ImapMailStore`` implements ``MailStore``.

Contracts (design: "Architecture" and "Reliability and data handling"):

* **Pool.** At most ``account.pool_size`` authenticated connections. A connection
  is checked out by exactly one thread at a time and remembers which mailbox is
  selected (and read-only vs read-write) so consecutive operations reuse it. IDLE
  runs on one dedicated connection that is never part of the pool.
* **Addressing.** Every ``(mailbox, uidvalidity, uids)`` call selects the mailbox,
  compares UIDVALIDITY and raises ``STALE_HANDLE`` on mismatch. UIDs only.
* **No implicit \\Seen.** Reads use ``BODY.PEEK``. Pure reads use EXAMINE, writes
  use SELECT; a read-write selection also serves later reads.
* **Reconnect.** Idempotent reads (and flag set/clear) are retried once on a fresh
  connection after a connection error. Other mutations are never re-sent once the
  command may have reached the server: they raise ``BRIDGE_UNAVAILABLE`` and the
  caller reconciles. Capabilities are rediscovered on every new connection.
* **Expunge.** Only ``UID EXPUNGE`` (UIDPLUS); a bare ``EXPUNGE`` is never issued,
  because it would also remove unrelated \\Deleted messages.
* **Privacy.** Nothing logs credentials, subjects, addresses or bodies.

Advertised capabilities are not evidence of working Bridge behaviour; the report
says so until live acceptance tests exist.
"""

from __future__ import annotations

import contextlib
import imaplib
import logging
import re
import select
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.header import decode_header, make_header
from email.parser import BytesHeaderParser
from typing import Any

from imapclient import IMAPClient
from imapclient.datetime_util import format_criteria_date
from imapclient.imapclient import SocketTimeout
from imapclient.response_parser import parse_message_list

from ..config import AccountConfig, Security
from ..domain.errors import ErrorCode, MailError, invalid, not_found, stale
from ..domain.models import (
    Address,
    MailboxInfo,
    MailboxRole,
    MessageHandle,
    MessageSummary,
    SearchQuery,
)
from . import credentials, tls
from .ports import AppendResult, CapabilityReport, CopyResult, FetchedMessage

logger = logging.getLogger(__name__)

OP_TIMEOUT = 60.0  # socket read timeout once connected
SELECT_TTL = 30.0  # re-SELECT after this long so UIDVALIDITY changes are noticed
HEALTH_CHECK_AFTER = 10.0  # NOOP a pooled connection idle longer than this
POOL_WAIT = 60.0
UID_CHUNK = 500

_CONN_ERRORS = (OSError, EOFError, imaplib.IMAP4.abort)

FEATURES = ("UIDPLUS", "MOVE", "IDLE", "ID", "NAMESPACE", "CONDSTORE", "QRESYNC", "SORT",
            "THREAD", "QUOTA", "SPECIAL-USE", "LIST-STATUS", "ESEARCH", "UNSELECT", "LITERAL+")

_SPECIAL_USE_ROLES = {
    "\\sent": MailboxRole.SENT,
    "\\drafts": MailboxRole.DRAFTS,
    "\\trash": MailboxRole.TRASH,
    "\\junk": MailboxRole.SPAM,
    "\\archive": MailboxRole.ARCHIVE,
    "\\all": MailboxRole.ALL_MAIL,
    "\\flagged": MailboxRole.STARRED,
}
_BRIDGE_NAME_ROLES = {
    "inbox": MailboxRole.INBOX,
    "sent": MailboxRole.SENT,
    "drafts": MailboxRole.DRAFTS,
    "trash": MailboxRole.TRASH,
    "spam": MailboxRole.SPAM,
    "archive": MailboxRole.ARCHIVE,
    "all mail": MailboxRole.ALL_MAIL,
    "starred": MailboxRole.STARRED,
    "scheduled": MailboxRole.SCHEDULED,
}
_LOOKUP_ROLES = (MailboxRole.INBOX, MailboxRole.SENT, MailboxRole.DRAFTS, MailboxRole.TRASH,
                 MailboxRole.SPAM, MailboxRole.ARCHIVE, MailboxRole.ALL_MAIL,
                 MailboxRole.STARRED, MailboxRole.SCHEDULED)

_FLAG_RE = re.compile(r"^\\?[^\s(){%*\"\\\]\[\x00-\x1f]+$")
_UID_RANGE_RE = re.compile(r"^[0-9*:,]+$")


# ------------------------------------------------------------------ search


def build_search_criteria(query: SearchQuery) -> list[Any]:
    """Translate a structured query to an IMAPClient criteria list.

    Leaves are AND-ed; ``any_of`` becomes right-nested ``OR`` of parenthesized
    groups; ``not_`` becomes ``NOT (group)``. Nested lists are parenthesized by
    IMAPClient. An empty query yields ``[]`` (the caller searches ALL).
    """
    out: list[Any] = []
    for attr, key in (("from_", "FROM"), ("to", "TO"), ("cc", "CC"), ("bcc", "BCC"),
                      ("subject", "SUBJECT"), ("body", "BODY"), ("text", "TEXT")):
        value = getattr(query, attr)
        if value is not None:
            out += [key, value]
    if query.since is not None:
        out += ["SINCE", query.since.date()]
    if query.before is not None:
        out += ["BEFORE", query.before.date()]
    if query.larger_than is not None:
        out += ["LARGER", int(query.larger_than)]
    if query.smaller_than is not None:
        out += ["SMALLER", int(query.smaller_than)]
    for value, yes, no in ((query.seen, "SEEN", "UNSEEN"),
                           (query.flagged, "FLAGGED", "UNFLAGGED"),
                           (query.answered, "ANSWERED", "UNANSWERED"),
                           (query.draft, "DRAFT", "UNDRAFT"),
                           (query.deleted, "DELETED", "UNDELETED")):
        if value is not None:
            out.append(yes if value else no)
    if query.keyword is not None:
        _check_flag(query.keyword, allow_system=False)
        out += ["KEYWORD", query.keyword]
    for name, value in (query.header or {}).items():
        out += ["HEADER", name, value]
    if query.uid_range is not None:
        if not _UID_RANGE_RE.match(query.uid_range):
            raise invalid("uid_range must be a UID set such as '1:10,15'")
        out += ["UID", query.uid_range]
    if query.any_of:
        groups = [build_search_criteria(q) or ["ALL"] for q in query.any_of]
        expr: list[Any] = groups[-1]
        for group in reversed(groups[:-1]):
            expr = ["OR", group, expr]
        out.extend(expr)
    if query.not_ is not None:
        out += ["NOT", build_search_criteria(query.not_) or ["ALL"]]
    return out


def search_notes(query: SearchQuery) -> list[str]:
    """Disclose anything the translation or Bridge may approximate."""
    notes: list[str] = []
    found = _walk_query(query)
    if any(q.since or q.before for q in found):
        notes.append("SINCE/BEFORE use day granularity (time of day is ignored by IMAP).")
    if any(q.body or q.text for q in found):
        notes.append("Body/text search depends on Bridge's local index and may be incomplete.")
    if any(_has_non_ascii(build_search_criteria(q)) for q in found):
        notes.append("Non-ASCII criteria were sent with CHARSET UTF-8; server support is "
                     "unverified.")
    return notes


def _walk_query(query: SearchQuery) -> list[SearchQuery]:
    out = [query]
    for sub in query.any_of or []:
        out += _walk_query(sub)
    if query.not_ is not None:
        out += _walk_query(query.not_)
    return out


def _has_non_ascii(criteria: Any) -> bool:
    if isinstance(criteria, str):
        return not criteria.isascii()
    if isinstance(criteria, list):
        return any(_has_non_ascii(c) for c in criteria)
    return False


_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")  # tab is the only allowed control char
_QUOTE_CHARS = re.compile(r'[\x00-\x20"\\(){%*\]]')  # atom-specials (RFC 3501)


def encode_criteria(criteria: list[Any]) -> list[bytes]:
    """Flatten criteria to IMAP tokens (parentheses are separate tokens).

    IMAPClient's own normaliser glues ")" onto the last item of a group, which
    corrupts a trailing non-ASCII literal, and ignores the charset for nested
    groups. 8-bit values stay unquoted so IMAPClient sends them as literals.
    """
    out: list[bytes] = []
    for item in criteria:
        if isinstance(item, (str, bytes)) and _CONTROL.search(
                item if isinstance(item, str) else item.decode("latin-1")):
            # CR/LF would end the command line and let the value inject IMAP commands.
            raise invalid("search values must not contain control characters")
        if out and out[-1] == b"UID" and isinstance(item, str):
            out.append(item.encode("ascii"))  # a sequence set is an atom ("5:*")
        elif isinstance(item, list):
            out += [b"(", *encode_criteria(item), b")"]
        elif isinstance(item, int):
            out.append(str(item).encode("ascii"))
        elif isinstance(item, (date, datetime)):
            out.append(format_criteria_date(item))
        else:
            raw = item if isinstance(item, bytes) else str(item).encode("utf-8")
            if raw.isascii() and (not raw or _QUOTE_CHARS.search(raw.decode("ascii"))):
                raw = b'"' + raw.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'
            out.append(raw)
    return out


def _check_flag(flag: str, *, allow_system: bool = True) -> None:
    if not _FLAG_RE.match(flag) or (flag.startswith("\\") and not allow_system):
        raise invalid("invalid flag or keyword", flag=flag[:40])


def _is_no_such_message(low: str) -> bool:
    """Bridge's rejection of commands naming missing UIDs ("no such message")."""
    return "no such message" in low


def _uid_set(uids: list[int]) -> str:
    """Compress sorted UIDs to ranges ('1:3,7')."""
    nums = sorted({int(u) for u in uids})
    parts: list[str] = []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        parts.append(str(nums[i]) if i == j else f"{nums[i]}:{nums[j]}")
        i = j + 1
    return ",".join(parts)


def _expand_uid_set(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        if ":" in part:
            lo, hi = (int(x) for x in part.split(":", 1))
            out.extend(range(lo, hi + 1) if lo <= hi else range(lo, hi - 1, -1))
        elif part:
            out.append(int(part))
    return out


def _chunks(items: list[int], size: int = UID_CHUNK) -> list[list[int]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


# ------------------------------------------------------------------ decoding


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _decode_header_text(value: Any) -> str | None:
    """RFC 2047 decode, tolerating unknown charsets and raw 8-bit headers."""
    text = _text(value)
    if text is None:
        return None
    text = re.sub(r"\r?\n[ \t]+", " ", text)
    try:
        return str(make_header(decode_header(text)))
    except (LookupError, ValueError, UnicodeError):
        return text


def _addresses(items: Any) -> list[Address]:
    out: list[Address] = []
    for a in items or ():
        mailbox, host = _text(a.mailbox), _text(a.host)
        if not mailbox or not host:
            continue  # group syntax markers
        try:
            out.append(Address(name=_decode_header_text(a.name) or None,
                               email=f"{mailbox}@{host}"))
        except ValueError:
            continue
    return out


def _flags(raw: Any) -> list[str]:
    names = [f.decode() if isinstance(f, bytes) else str(f) for f in (raw or ())]
    return [n for n in names if n.lower() != "\\recent"]  # \\Recent is per-session noise


def _has_attachments(structure: Any) -> bool | None:
    """Best effort from BODYSTRUCTURE; None when the structure is unrecognised."""
    try:
        return _walk_structure(structure, parent_subtype="", first_text=[True])
    except (TypeError, ValueError, IndexError, AttributeError):
        return None


def _walk_structure(part: Any, parent_subtype: str, first_text: list[bool]) -> bool:
    if isinstance(part[0], list):  # multipart: (children, subtype, ...)
        subtype = (_text(part[1]) or "").lower()
        return any(_walk_structure(child, subtype, first_text) for child in part[0])
    maintype = (_text(part[0]) or "").lower()
    subtype = (_text(part[1]) or "").lower()
    for item in part[7:]:
        if isinstance(item, tuple) and item and isinstance(item[0], bytes):
            disposition = item[0].decode("ascii", "replace").lower()
            if disposition == "attachment":
                return True
    if maintype == "message" and subtype == "rfc822":
        return True
    if maintype == "text" and subtype in ("plain", "html") and first_text[0]:
        first_text[0] = False
        return False
    return parent_subtype == "mixed"


# ------------------------------------------------------------------ connections


@dataclass
class _Selection:
    mailbox: str
    uidvalidity: int
    uidnext: int | None
    exists: int | None
    permanent_flags: frozenset[str] | None  # lower-cased; None when not reported
    writable: bool
    epoch: int
    at: float


@dataclass
class _Conn:
    client: Any
    caps: frozenset[str]
    last_used: float
    sel: _Selection | None = None


@dataclass
class _Folder:
    name: str
    attributes: list[str]
    delimiter: str | None


def _has_cap(caps: frozenset[str] | set[str], name: str) -> bool:
    name = name.upper()
    return name in caps or any(c.startswith(name + "=") for c in caps)


class ImapMailStore:
    """Thread-safe ``MailStore`` over a bounded IMAP connection pool."""

    def __init__(self, account: AccountConfig,
                 password_provider: Callable[[], str] | None = None) -> None:
        self._acct = account
        self.account = account.name
        self._password_provider = password_provider or (
            lambda: credentials.resolve_secret(account.secret_ref))
        self._cv = threading.Condition()
        self._idle_pool: list[_Conn] = []
        self._total = 0
        self._closed = False
        self._epoch = 0  # bumped on rename/delete so cached selections are dropped
        self._state = threading.RLock()  # guards the caches below
        self._caps: frozenset[str] | None = None
        self._report: CapabilityReport | None = None
        self._server_id: dict[str, str] | None = None
        self._folders: dict[str, _Folder] | None = None
        self._delimiter: str | None = None
        self._idle_lock = threading.Lock()
        self._idle_conn: _Conn | None = None

    # ------------------------------------------------------------ connect

    def _connect(self) -> _Conn:
        """Connect, retrying once if the peer resets the connection mid-handshake
        (login is not a mutation, so this is safe)."""
        try:
            return self._connect_once()
        except MailError as exc:
            if not exc.details.get("transient"):
                raise
        time.sleep(0.2)
        return self._connect_once()

    def _connect_once(self) -> _Conn:
        acct = self._acct
        sec = acct.imap_security
        tls.require_transport_security(acct.imap_host, sec)
        password = self._password_provider()
        ctx = None if sec is Security.NONE else tls.make_ssl_context(acct)
        client: Any = None
        stage = "connect"
        try:
            client = IMAPClient(
                acct.imap_host, port=acct.imap_port, ssl=sec is Security.SSL,
                ssl_context=ctx if sec is Security.SSL else None,
                timeout=SocketTimeout(acct.connect_timeout, OP_TIMEOUT))
            client._imap.debug = 0  # imaplib debug output would include LOGIN/bodies
            client.normalise_times = False  # keep server UTC offsets
            stage = "tls"
            if sec is Security.STARTTLS:
                client.starttls(ctx)
            if sec is not Security.NONE:
                tls.verify_peer(acct, client.socket())
            stage = "login"
            client.login(acct.username, password)
            caps = frozenset(c.decode("ascii", "replace").upper()
                             for c in client.capabilities())
        except MailError:
            self._quiet_close(client)
            raise
        except ssl.SSLError as exc:
            self._quiet_close(client)
            raise MailError(ErrorCode.TLS_ERROR, "TLS handshake with Bridge failed",
                            reason=type(exc).__name__) from exc
        except imaplib.IMAP4.error as exc:  # LoginError, abort, refused STARTTLS
            self._quiet_close(client)
            if stage == "tls":
                raise MailError(ErrorCode.TLS_ERROR, "Bridge refused STARTTLS") from exc
            if isinstance(exc, imaplib.IMAP4.abort):
                raise MailError(ErrorCode.BRIDGE_UNAVAILABLE, "Bridge closed the connection",
                                transient=True) from exc
            raise MailError(ErrorCode.AUTH_FAILED, "Bridge rejected the credentials") from exc
        except (OSError, EOFError) as exc:
            self._quiet_close(client)
            raise MailError(ErrorCode.BRIDGE_UNAVAILABLE, "cannot reach Bridge IMAP",
                            reason=type(exc).__name__,
                            transient=isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                                                       BrokenPipeError, EOFError))) from exc
        with self._state:
            self._caps = caps
        logger.debug("imap connected account=%s caps=%d", self.account, len(caps))
        return _Conn(client, caps, time.monotonic())

    @staticmethod
    def _quiet_close(client: Any) -> None:
        if client is None:
            return
        with contextlib.suppress(Exception):  # best-effort teardown of a possibly dead socket
            client.logout()
            return
        with contextlib.suppress(Exception):
            client.socket().close()

    # ------------------------------------------------------------ pool

    def _checkout(self) -> _Conn:
        deadline = time.monotonic() + POOL_WAIT
        with self._cv:
            while True:
                if self._closed:
                    raise MailError(ErrorCode.INTERNAL, "mail store is closed")
                if self._idle_pool:
                    conn: _Conn | None = self._idle_pool.pop()
                    break
                if self._total < self._acct.pool_size:
                    self._total += 1
                    conn = None
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MailError(ErrorCode.BRIDGE_UNAVAILABLE, "IMAP connection pool exhausted")
                self._cv.wait(remaining)
        try:
            if conn is not None and time.monotonic() - conn.last_used > HEALTH_CHECK_AFTER:
                try:
                    conn.client.noop()
                except (*_CONN_ERRORS, imaplib.IMAP4.error):
                    self._quiet_close(conn.client)
                    with self._state:
                        self._report = None
                    conn = None  # slot is kept; reconnect below
            if conn is None:
                conn = self._connect()
            return conn
        except BaseException:
            with self._cv:
                self._total -= 1
                self._cv.notify()
            raise

    def _checkin(self, conn: _Conn, broken: bool) -> None:
        if broken:
            self._quiet_close(conn.client)
            with self._state:
                self._report = None  # capabilities are rediscovered on reconnect
        with self._cv:
            if broken or self._closed:
                if not broken:
                    self._quiet_close(conn.client)
                self._total -= 1
            else:
                conn.last_used = time.monotonic()
                self._idle_pool.append(conn)
            self._cv.notify()

    def _exec(self, fn: Callable[[_Conn], Any], *, retry: bool, what: str = "operation") -> Any:
        attempts = 2 if retry else 1
        last: BaseException | None = None
        for attempt in range(attempts):
            conn = self._checkout()
            broken = False
            try:
                return fn(conn)
            except MailError:
                raise
            except imaplib.IMAP4.readonly as exc:
                raise self._translate(exc, what) from exc
            except _CONN_ERRORS as exc:
                broken = True
                last = exc
                logger.debug("imap connection error during %s: %s", what, type(exc).__name__)
            except imaplib.IMAP4.error as exc:
                raise self._translate(exc, what) from exc
            except Exception:
                broken = True
                raise
            finally:
                self._checkin(conn, broken)
            if attempt + 1 < attempts:
                continue
        if retry:
            raise MailError(ErrorCode.BRIDGE_UNAVAILABLE,
                            f"lost the Bridge connection during {what}") from last
        raise MailError(ErrorCode.BRIDGE_UNAVAILABLE,
                        f"lost the Bridge connection during {what}; the change may or may "
                        "not have been applied", outcome_unknown=True) from last

    @staticmethod
    def _translate(exc: BaseException, what: str) -> MailError:
        text = str(exc)
        low = text.lower()
        if isinstance(exc, imaplib.IMAP4.readonly):
            return MailError(ErrorCode.UNSUPPORTED, "mailbox is read-only")
        if any(k in low for k in ("alreadyexists", "already exists")):
            return MailError(ErrorCode.CONFLICT, f"{what}: mailbox already exists")
        if _is_no_such_message(low):
            return MailError(ErrorCode.NOT_FOUND, f"{what}: message no longer exists")
        if any(k in low for k in ("trycreate", "nonexistent", "does not exist", "doesn't exist",
                                  "no such", "not found", "unknown mailbox")):
            return MailError(ErrorCode.NOT_FOUND, f"{what}: mailbox not found")
        return MailError(ErrorCode.INTERNAL, f"IMAP server rejected {what}", detail=text[:200])

    # ------------------------------------------------------------ selection

    def _select(self, conn: _Conn, mailbox: str, *, write: bool, force: bool = False
                ) -> _Selection:
        sel = conn.sel
        if (not force and sel is not None and sel.mailbox == mailbox
                and sel.epoch == self._epoch and time.monotonic() - sel.at < SELECT_TTL
                and (sel.writable or not write)):
            return sel
        conn.sel = None
        try:
            info = conn.client.select_folder(mailbox, readonly=not write)
        except imaplib.IMAP4.abort:
            raise
        except imaplib.IMAP4.error as exc:
            raise self._translate(exc, "select") from exc
        writable = bool(info.get(b"READ-WRITE")) and write
        if write and not info.get(b"READ-WRITE"):
            raise MailError(ErrorCode.UNSUPPORTED, "mailbox is read-only on the server")
        perm = info.get(b"PERMANENTFLAGS")
        sel = _Selection(
            mailbox=mailbox,
            uidvalidity=int(info.get(b"UIDVALIDITY") or 0),
            uidnext=info.get(b"UIDNEXT"),
            exists=info.get(b"EXISTS"),
            permanent_flags=(frozenset(f.decode("utf-8", "replace").lower() for f in perm)
                             if perm else None),
            writable=writable,
            epoch=self._epoch,
            at=time.monotonic(),
        )
        conn.sel = sel
        return sel

    def _open(self, conn: _Conn, mailbox: str, uidvalidity: int | None, *, write: bool
              ) -> _Selection:
        """Select and verify UIDVALIDITY (re-selecting once if the cached one differs)."""
        sel = self._select(conn, mailbox, write=write)
        if uidvalidity is not None and sel.uidvalidity != uidvalidity:
            if time.monotonic() - sel.at > 0.5:
                sel = self._select(conn, mailbox, write=write, force=True)
            if sel.uidvalidity != uidvalidity:
                raise stale("mailbox UIDVALIDITY changed; message handles are no longer valid",
                            mailbox=mailbox)
        return sel

    # ------------------------------------------------------------ discovery

    def _ensure_caps(self) -> frozenset[str]:
        with self._state:
            caps = self._caps
        if caps is None:
            self._exec(lambda c: None, retry=True, what="connect")
            with self._state:
                caps = self._caps
        return caps or frozenset()

    def has_capability(self, name: str) -> bool:
        return _has_cap(self._ensure_caps(), name)

    def _load_folders(self, conn: _Conn) -> dict[str, _Folder]:
        raw = conn.client.list_folders()
        folders: dict[str, _Folder] = {}
        delimiter: str | None = None
        for flags, delim, name in raw:
            d = _text(delim)
            delimiter = delimiter or d
            folders[name] = _Folder(name, [_text(f) or "" for f in flags], d)
        with self._state:
            self._folders = folders
            self._delimiter = delimiter or self._delimiter
        return folders

    def _folder_map(self) -> dict[str, _Folder]:
        with self._state:
            if self._folders is not None:
                return self._folders
        return self._exec(self._load_folders, retry=True, what="list mailboxes")

    def _invalidate_folders(self) -> None:
        with self._state:
            self._folders = None
            self._report = None

    def _role(self, name: str, attributes: list[str], delimiter: str | None) -> MailboxRole:
        attrs = {a.lower() for a in attributes}
        for attr, role in _SPECIAL_USE_ROLES.items():
            if attr in attrs:
                return role
        delim = delimiter or self._delimiter or "/"
        low = name.lower()
        if low in _BRIDGE_NAME_ROLES:
            return _BRIDGE_NAME_ROLES[low]
        if name in ("Folders", "Labels"):
            return MailboxRole.CONTAINER
        if name.startswith("Folders" + delim):
            return MailboxRole.FOLDER
        if name.startswith("Labels" + delim):
            return MailboxRole.LABEL
        if "\\noselect" in attrs:
            return MailboxRole.CONTAINER
        return MailboxRole.OTHER

    def role_of(self, mailbox: str) -> MailboxRole:
        folder = self._folder_map().get(mailbox)
        if folder is None:
            return self._role(mailbox, [], None)
        return self._role(folder.name, folder.attributes, folder.delimiter)

    def mailbox_for_role(self, role: MailboxRole) -> str | None:
        if role is MailboxRole.INBOX:
            return "INBOX"
        if role not in _LOOKUP_ROLES:
            return None
        matches = [f for f in self._folder_map().values()
                   if self._role(f.name, f.attributes, f.delimiter) is role]
        # prefer a server-declared special-use folder over a name match
        matches.sort(key=lambda f: not any(a.lower() in _SPECIAL_USE_ROLES for a in f.attributes))
        return matches[0].name if matches else None

    def capabilities(self, refresh: bool = False) -> CapabilityReport:
        with self._state:
            if self._report is not None and not refresh:
                return self._report
        report = self._exec(self._build_report, retry=True, what="capability discovery")
        with self._state:
            self._report = report
        return report

    def _build_report(self, conn: _Conn) -> CapabilityReport:
        caps = conn.caps
        notes = ["Capabilities are as advertised by the server. Bridge behaviour for each "
                 "feature is unverified until live acceptance tests have run."]
        server_id: dict[str, str] | None = None
        if _has_cap(caps, "ID"):
            try:
                server_id = _parse_id(conn.client.id_())
            except imaplib.IMAP4.error:
                notes.append("ID is advertised but the ID command failed.")
        self._load_folders(conn)
        with self._state:
            self._server_id = server_id
        special = {}
        for role in _LOOKUP_ROLES:
            name = self.mailbox_for_role(role)
            if name is not None:
                special[role.value] = name
        if not _has_cap(caps, "SPECIAL-USE"):
            notes.append("SPECIAL-USE is not advertised; roles are inferred from Bridge "
                         "mailbox names.")
        if not _has_cap(caps, "UIDPLUS"):
            notes.append("UIDPLUS missing: targeted expunge is unavailable and copy/append "
                         "cannot report destination UIDs.")
        return CapabilityReport(
            account=self.account,
            server_capabilities=sorted(caps),
            features={f: "available" if _has_cap(caps, f) else "unavailable" for f in FEATURES},
            special_folders=special,
            delimiter=self._delimiter,
            server_id=server_id,
            checked_at=datetime.now(UTC),
            notes=notes,
        )

    def list_mailboxes(self, with_counts: bool = False) -> list[MailboxInfo]:
        def run(conn: _Conn) -> list[MailboxInfo]:
            folders = self._load_folders(conn)
            subscribed = {name for _, _, name in conn.client.list_sub_folders()}
            out: list[MailboxInfo] = []
            for f in folders.values():
                low = {a.lower() for a in f.attributes}
                selectable = "\\noselect" not in low and "\\nonexistent" not in low
                info = MailboxInfo(
                    account=self.account, name=f.name, delimiter=f.delimiter,
                    attributes=f.attributes, role=self._role(f.name, f.attributes, f.delimiter),
                    selectable=selectable, subscribed=f.name in subscribed)
                if with_counts and selectable:
                    try:
                        st = conn.client.folder_status(
                            f.name, ["MESSAGES", "UNSEEN", "UIDVALIDITY", "UIDNEXT"])
                    except imaplib.IMAP4.abort:
                        raise
                    except imaplib.IMAP4.error:
                        logger.debug("STATUS failed for a mailbox")
                    else:
                        info.messages = st.get(b"MESSAGES")
                        info.unseen = st.get(b"UNSEEN")
                        info.uidvalidity = st.get(b"UIDVALIDITY")
                        info.uidnext = st.get(b"UIDNEXT")
                out.append(info)
            return out

        return self._exec(run, retry=True, what="list mailboxes")

    def mailbox_status(self, mailbox: str) -> MailboxInfo:
        def run(conn: _Conn) -> tuple[_Selection, int | None]:
            try:
                sel = self._select(conn, mailbox, write=True, force=True)
            except MailError as exc:
                if exc.code is not ErrorCode.UNSUPPORTED:
                    raise
                sel = self._select(conn, mailbox, write=False, force=True)  # read-only mailbox
            unseen = None
            try:
                unseen = conn.client.folder_status(mailbox, ["UNSEEN"]).get(b"UNSEEN")
            except imaplib.IMAP4.abort:
                raise
            except imaplib.IMAP4.error:
                pass
            return sel, unseen

        sel, unseen = self._exec(run, retry=True, what="mailbox status")
        folder = self._folder_map().get(mailbox)
        attrs = folder.attributes if folder else []
        delim = folder.delimiter if folder else None
        return MailboxInfo(
            account=self.account, name=mailbox, delimiter=delim, attributes=attrs,
            role=self._role(mailbox, attrs, delim), selectable=True, messages=sel.exists,
            unseen=unseen, uidvalidity=sel.uidvalidity, uidnext=sel.uidnext,
            writable=sel.writable,
            permanent_flags=(sorted(sel.permanent_flags) if sel.permanent_flags is not None
                             else None))

    # ------------------------------------------------------------ reading

    def list_uids(self, mailbox: str, criteria: list[object] | None = None
                  ) -> tuple[int, list[int]]:
        crit = list(criteria) if criteria else ["ALL"]
        charset = "UTF-8" if _has_non_ascii(crit) else None
        args = ([b"CHARSET", charset.encode()] if charset else []) + encode_criteria(crit)

        def run(conn: _Conn) -> tuple[int, list[int]]:
            sel = self._open(conn, mailbox, None, write=False)
            data = conn.client._raw_command_untagged(b"SEARCH", args)
            return sel.uidvalidity, sorted(parse_message_list(data))

        return self._exec(run, retry=True, what="search")

    def search(self, mailbox: str, query: SearchQuery) -> tuple[int, list[int], list[str]]:
        uidvalidity, uids = self.list_uids(mailbox, build_search_criteria(query) or None)
        return uidvalidity, uids, search_notes(query)

    def find_by_message_id(self, mailbox: str, message_id: str) -> tuple[int, list[int]]:
        return self.list_uids(mailbox, ["HEADER", "Message-ID", message_id.strip()])

    def fetch_summaries(self, mailbox: str, uidvalidity: int, uids: list[int]
                        ) -> list[MessageSummary]:
        if not uids:
            return []
        fields = ["FLAGS", "INTERNALDATE", "RFC822.SIZE", "ENVELOPE", "BODYSTRUCTURE"]

        def run(conn: _Conn) -> dict[int, dict[bytes, Any]]:
            self._open(conn, mailbox, uidvalidity, write=False)
            data: dict[int, dict[bytes, Any]] = {}
            for chunk in _chunks(list(uids)):
                data.update(self._fetch_present(conn, chunk, fields))
            return data

        data = self._exec(run, retry=True, what="fetch")
        out: list[MessageSummary] = []
        for uid in uids:
            item = data.get(uid)
            if item is None:
                continue  # expunged since it was listed
            env = item.get(b"ENVELOPE")
            handle = MessageHandle(account=self.account, mailbox=mailbox,
                                   uidvalidity=uidvalidity, uid=uid)
            out.append(MessageSummary(
                handle=handle.token(), mailbox=mailbox, uid=uid,
                message_id=_text(env.message_id) if env else None,
                subject=_decode_header_text(env.subject) if env else None,
                **{"from": _addresses(env.from_) if env else []},
                to=_addresses(env.to) if env else [],
                cc=_addresses(env.cc) if env else [],
                date=env.date if env else None,
                internal_date=item.get(b"INTERNALDATE"),
                size=item.get(b"RFC822.SIZE"),
                flags=_flags(item.get(b"FLAGS")),
                has_attachments=(_has_attachments(item[b"BODYSTRUCTURE"])
                                 if b"BODYSTRUCTURE" in item else None),
            ))
        return out

    def fetch_message(self, mailbox: str, uidvalidity: int, uid: int,
                      header_only: bool = False) -> FetchedMessage:
        section = "BODY.PEEK[HEADER]" if header_only else "BODY.PEEK[]"

        def run(conn: _Conn) -> dict[int, dict[bytes, Any]]:
            self._open(conn, mailbox, uidvalidity, write=False)
            return ImapMailStore._fetch_present(
                conn, [uid], ["FLAGS", "INTERNALDATE", "RFC822.SIZE", section])

        item = self._exec(run, retry=True, what="fetch").get(uid)
        if item is None:
            raise not_found("message no longer exists in this mailbox")
        raw = next((v for k, v in item.items() if k.startswith(b"BODY[")), None)
        if raw is None:
            raise MailError(ErrorCode.INTERNAL, "server returned no message data")
        return FetchedMessage(
            mailbox=mailbox, uidvalidity=uidvalidity, uid=uid, flags=_flags(item.get(b"FLAGS")),
            internal_date=item.get(b"INTERNALDATE"), size=item.get(b"RFC822.SIZE"),
            raw=bytes(raw), header_only=header_only)

    def fetch_flags(self, mailbox: str, uidvalidity: int, uids: list[int]
                    ) -> dict[int, list[str]]:
        if not uids:
            return {}

        def run(conn: _Conn) -> dict[int, list[str]]:
            self._open(conn, mailbox, uidvalidity, write=False)
            return self._flags_of(conn, uids)

        return self._exec(run, retry=True, what="fetch flags")

    @staticmethod
    def _flags_of(conn: _Conn, uids: list[int]) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {}
        for chunk in _chunks(list(uids)):
            for uid, item in ImapMailStore._fetch_present(conn, chunk, ["FLAGS"]).items():
                out[uid] = _flags(item.get(b"FLAGS"))
        return out

    @staticmethod
    def _fetch_present(conn: _Conn, uids: list[int], fields: list[str]
                       ) -> dict[int, dict[bytes, Any]]:
        """UID FETCH that tolerates UIDs that no longer exist.

        RFC 3501 servers answer a UID FETCH naming missing UIDs with OK and no data.
        Proton Bridge instead rejects the whole command with ``NO ... no such message``
        (observed live, 2026-10-09). Reads are side-effect free, so on that error retry
        once with only the UIDs still present.
        """
        try:
            return dict(conn.client.fetch(uids, fields))
        except imaplib.IMAP4.error as exc:
            if isinstance(exc, imaplib.IMAP4.abort) or not _is_no_such_message(str(exc).lower()):
                raise
        present = sorted(ImapMailStore._present(conn) & set(uids))
        return dict(conn.client.fetch(present, fields)) if present else {}

    @staticmethod
    def _present(conn: _Conn) -> set[int]:
        """All UIDs in the selected mailbox. Never names a specific UID, so it cannot
        trip Bridge's rejection of searches for missing UIDs."""
        return {int(u) for u in conn.client.search(["ALL"])}

    # ------------------------------------------------------------ mutation

    def _check_flags_permitted(self, sel: _Selection, flags: list[str]) -> None:
        for flag in flags:
            _check_flag(flag)
        perm = sel.permanent_flags
        if perm is None:
            return
        for flag in flags:
            low = flag.lower()
            if low == "\\recent":
                raise MailError(ErrorCode.UNSUPPORTED, "\\Recent cannot be set by clients")
            ok = low in perm if low.startswith("\\") else ("\\*" in perm or low in perm)
            if not ok:
                raise MailError(ErrorCode.UNSUPPORTED,
                                "flag not permitted by this mailbox (PERMANENTFLAGS)",
                                flag=flag)

    def store_flags(self, mailbox: str, uidvalidity: int, uids: list[int],
                    add: list[str] | None = None, remove: list[str] | None = None
                    ) -> dict[int, list[str]]:
        add, remove = list(add or []), list(remove or [])
        if not uids or not (add or remove):
            return self.fetch_flags(mailbox, uidvalidity, uids) if uids else {}

        def run(conn: _Conn) -> dict[int, list[str]]:
            sel = self._open(conn, mailbox, uidvalidity, write=True)
            self._check_flags_permitted(sel, add + remove)
            for chunk in _chunks(list(uids)):
                if add:
                    conn.client.add_flags(chunk, add, silent=True)
                if remove:
                    conn.client.remove_flags(chunk, remove, silent=True)
            return self._flags_of(conn, uids)

        # +FLAGS/-FLAGS are idempotent, so a retry after reconnect is safe.
        return self._exec(run, retry=True, what="store flags")

    def _uid_command(self, conn: _Conn, command: str, uids: list[int], dest: str
                     ) -> tuple[int | None, dict[int, int]]:
        """Run COPY/MOVE and return (dest uidvalidity, src->dst) from COPYUID."""
        imap = conn.client._imap
        mapping: dict[int, int] = {}
        dest_uv: int | None = None
        for chunk in _chunks(list(uids)):
            imap.untagged_responses.pop("COPYUID", None)
            if command == "MOVE":
                conn.client.move(chunk, dest)
            else:
                conn.client.copy(chunk, dest)
            for raw in imap.untagged_responses.pop("COPYUID", []):
                try:
                    uv, src, dst = raw.decode("ascii").split()
                    srcs, dsts = _expand_uid_set(src), _expand_uid_set(dst)
                except ValueError:
                    continue
                dest_uv = int(uv)
                mapping.update(zip(srcs, dsts, strict=False))
        return dest_uv, mapping

    def copy(self, mailbox: str, uidvalidity: int, uids: list[int], dest: str) -> CopyResult:
        def run(conn: _Conn) -> CopyResult:
            self._open(conn, mailbox, uidvalidity, write=True)
            dest_uv, mapping = self._uid_command(conn, "COPY", uids, dest)
            return CopyResult({u: mapping.get(u) for u in uids}, dest_uv)

        if not uids:
            return CopyResult({}, None)
        return self._exec(run, retry=False, what="copy")

    def move(self, mailbox: str, uidvalidity: int, uids: list[int], dest: str) -> CopyResult:
        if not uids:
            return CopyResult({}, None)

        def run(conn: _Conn) -> CopyResult:
            self._open(conn, mailbox, uidvalidity, write=True)
            if _has_cap(conn.caps, "MOVE"):
                dest_uv, mapping = self._uid_command(conn, "MOVE", uids, dest)
                return CopyResult({u: mapping.get(u) for u in uids}, dest_uv)
            if not _has_cap(conn.caps, "UIDPLUS"):
                raise MailError(ErrorCode.CAPABILITY_UNAVAILABLE,
                                "move needs MOVE or UIDPLUS (safe COPY+UID EXPUNGE fallback)")
            dest_uv, mapping = self._uid_command(conn, "COPY", uids, dest)
            copied = [u for u in uids if u in mapping] if mapping else list(uids)
            for chunk in _chunks(copied):
                conn.client.add_flags(chunk, ["\\Deleted"], silent=True)
                conn.client.expunge(chunk)  # UID EXPUNGE <set>, never bare EXPUNGE
            return CopyResult({u: mapping.get(u) for u in uids}, dest_uv)

        return self._exec(run, retry=False, what="move")

    def append(self, mailbox: str, raw: bytes, flags: list[str] | None = None,
               internal_date: datetime | None = None) -> AppendResult:
        for flag in flags or []:
            _check_flag(flag)

        def run(conn: _Conn) -> tuple[int | None, int | None]:
            imap = conn.client._imap
            imap.untagged_responses.pop("APPENDUID", None)
            conn.client.append(mailbox, raw, flags or (), internal_date)
            for item in imap.untagged_responses.pop("APPENDUID", []):
                try:
                    uv, uid_set = item.decode("ascii").split()
                    return int(uv), _expand_uid_set(uid_set)[0]
                except (ValueError, IndexError):
                    continue
            return None, None

        uidvalidity, uid = self._exec(run, retry=False, what="append")
        if uid is not None:
            return AppendResult(mailbox, uidvalidity, uid)
        # Best effort without APPENDUID: locate by Message-ID (newest match wins).
        try:
            msg_id = BytesHeaderParser().parsebytes(raw[:65536]).get("Message-ID")
            if msg_id:
                uv, uids = self.find_by_message_id(mailbox, str(msg_id))
                if uids:
                    return AppendResult(mailbox, uv, max(uids))
                return AppendResult(mailbox, uv, None)
        except MailError:
            pass
        return AppendResult(mailbox, None, None)

    def expunge_uids(self, mailbox: str, uidvalidity: int, uids: list[int]) -> list[int]:
        if not uids:
            return []
        if not self.has_capability("UIDPLUS"):
            raise MailError(ErrorCode.CAPABILITY_UNAVAILABLE,
                            "targeted expunge requires UIDPLUS; bare EXPUNGE is never used")

        def run(conn: _Conn) -> list[int]:
            self._open(conn, mailbox, uidvalidity, write=True)
            wanted = sorted({int(u) for u in uids})
            before = self._existing(conn, wanted)
            for chunk in _chunks(wanted):
                conn.client.expunge(chunk)  # UID EXPUNGE <set>
            after = self._existing(conn, wanted)
            return sorted(before - after)

        return self._exec(run, retry=False, what="expunge")

    @staticmethod
    def _existing(conn: _Conn, uids: list[int]) -> set[int]:
        # Not "UID SEARCH UID <set>": Bridge answers NO when the set names a UID that
        # no longer exists, which is exactly the case after a successful expunge.
        return ImapMailStore._present(conn) & set(uids)

    def _mailbox_op(self, what: str, fn: Callable[[Any], Any], *, structural: bool = False
                    ) -> None:
        def run(conn: _Conn) -> None:
            if structural:
                # Servers may refuse to delete/rename the mailbox this session has
                # selected; EXAMINE INBOX leaves it without the implicit expunge of CLOSE.
                self._select(conn, "INBOX", write=False, force=True)
            fn(conn.client)

        self._exec(run, retry=False, what=what)
        self._invalidate_folders()
        if structural:
            with self._state:
                self._epoch += 1

    def create_mailbox(self, name: str) -> None:
        self._mailbox_op("create mailbox", lambda c: c.create_folder(name))

    def rename_mailbox(self, old: str, new: str) -> None:
        self._mailbox_op("rename mailbox", lambda c: c.rename_folder(old, new), structural=True)

    def delete_mailbox(self, name: str) -> None:
        self._mailbox_op("delete mailbox", lambda c: c.delete_folder(name), structural=True)

    def set_subscribed(self, name: str, subscribed: bool) -> None:
        def op(c: Any) -> None:
            (c.subscribe_folder if subscribed else c.unsubscribe_folder)(name)

        # SUBSCRIBE/UNSUBSCRIBE are idempotent
        self._exec(lambda conn: op(conn.client), retry=True, what="set subscription")

    # ------------------------------------------------------------ change tracking

    def idle_wait(self, mailbox: str, timeout: float) -> list[tuple[object, ...]]:
        """Block up to ``timeout`` seconds on the dedicated IDLE connection.

        Returns the raw untagged responses (possibly empty on timeout). Callers must
        re-sync after any return: events that arrived between calls are not replayed.
        """
        with self._idle_lock:
            for attempt in range(2):
                if self._closed:
                    raise MailError(ErrorCode.INTERNAL, "mail store is closed")
                conn = self._idle_conn
                try:
                    if conn is None:
                        conn = self._idle_conn = self._connect()
                    if not _has_cap(conn.caps, "IDLE"):
                        raise MailError(ErrorCode.CAPABILITY_UNAVAILABLE,
                                        "server does not advertise IDLE")
                    self._select(conn, mailbox, write=False)
                    conn.client.idle()
                except MailError:
                    raise
                except (*_CONN_ERRORS, imaplib.IMAP4.error) as exc:
                    self._drop_idle_conn()
                    if isinstance(exc, imaplib.IMAP4.error) and not isinstance(
                            exc, imaplib.IMAP4.abort):
                        raise self._translate(exc, "idle") from exc
                    if attempt == 0:
                        continue
                    raise MailError(ErrorCode.BRIDGE_UNAVAILABLE,
                                    "cannot start IDLE on Bridge") from exc
                break
            assert conn is not None
            client = conn.client
            sock = client.socket()
            try:
                # IDLE is only a wake-up signal. IMAPClient's idle_check() reads lines in
                # non-blocking mode and aborts on a response split across TCP segments
                # ("unterminated line"), so wait for readability here and let
                # idle_done() read everything in blocking mode, literals included.
                select.select([sock], [], [], max(timeout, 0))
                sock.settimeout(OP_TIMEOUT)
                _, responses = client.idle_done()
            except (*_CONN_ERRORS, imaplib.IMAP4.error) as exc:
                self._drop_idle_conn()
                raise MailError(ErrorCode.BRIDGE_UNAVAILABLE, "IDLE connection lost") from exc
            conn.last_used = time.monotonic()
            return [tuple(r) for r in responses]

    def _drop_idle_conn(self) -> None:
        conn, self._idle_conn = self._idle_conn, None
        if conn is not None:
            self._quiet_close(conn.client)

    # ------------------------------------------------------------ lifecycle

    def close(self) -> None:
        with self._cv:
            self._closed = True
            conns, self._idle_pool = self._idle_pool, []
            self._total -= len(conns)
            self._cv.notify_all()
        for conn in conns:
            self._quiet_close(conn.client)
        if self._idle_lock.acquire(blocking=False):
            try:
                self._drop_idle_conn()
            finally:
                self._idle_lock.release()
        else:  # an IDLE wait is in flight: break it by shutting the socket
            idle = self._idle_conn
            if idle is not None:
                with contextlib.suppress(OSError):
                    idle.client.socket().shutdown(socket.SHUT_RDWR)


def _parse_id(raw: Any) -> dict[str, str] | None:
    if not raw:
        return None
    if isinstance(raw, dict):
        pairs = list(raw.items())
    else:
        seq = [x for x in (raw[0] if len(raw) == 1 and isinstance(raw[0], (tuple, list))
                           else raw)]
        pairs = list(zip(seq[0::2], seq[1::2], strict=False))
    out = {_text(k) or "": _text(v) or "" for k, v in pairs}
    return out or None


def open_store(account: AccountConfig) -> ImapMailStore:
    """Build a store for ``account`` using its configured secret reference."""
    return ImapMailStore(account)
