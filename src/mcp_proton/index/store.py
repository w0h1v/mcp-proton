"""SQLite layer of the local index plus the retention, export and size controls.

Storage modes (``config.StorageMode``)

* ``live``      nothing is retained here. Every writer in this package refuses to run.
* ``metadata``  per-occurrence headers and flags (``idx_messages``) and a subject/address
                full-text row.
* ``index``     additionally message body text (and optionally extracted attachment
                text) in the FTS5 table ``idx_fts``.

Encryption at rest is NOT implemented. ``storage_report()`` states
``encrypted: false`` and this is a limitation to disclose, not a feature gap to
paper over. If optional encryption is added it must cover *all* retained
content: message bodies and extracted text, attachment artifacts, the SQLite
journal/WAL files (operation payloads can contain bodies of pending sends), and
every backup or export. Full-text search over encrypted storage needs a
deliberate design: an encrypted SQLite build (for example SQLCipher) keeps FTS5
working but moves the key handling problem to unattended start-up; encrypting
individual fields defeats FTS5 entirely. The file system permissions (0600) and
OS-level disk encryption are what protect this data today.

Purging deletes with ``PRAGMA secure_delete=ON`` (freed pages are overwritten),
optimizes the FTS5 index so deleted terms leave no segments, checkpoints and
truncates the WAL, then ``VACUUM``s. Bridge keeps its own cache and agents may
retain what they were shown; none of that is under this package's control.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import StorageMode
from ..domain.errors import ErrorCode, MailError
from ..domain.models import Address, MessageSummary
from ..domain.requests import CallerContext
from ..services.core import MailApp
from ..storage.db import Database
from . import migrations  # noqa: F401  (registers the index schema)

log = logging.getLogger(__name__)

TABLES = ("idx_messages", "idx_mailboxes", "idx_accounts", "saved_searches")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def require_enabled(app: MailApp) -> StorageMode:
    mode = app.config.storage_mode
    if mode is StorageMode.LIVE:
        raise MailError(ErrorCode.UNSUPPORTED,
                        "the local index is disabled (storage_mode=live); set storage_mode to "
                        "'metadata' or 'index' to enable it")
    return mode


def _require_owner(owner: CallerContext) -> None:
    if not owner.is_owner:
        raise MailError(ErrorCode.POLICY_DENIED, "only the owner can manage stored data")


def _addrs(items: Iterable[Address]) -> str:
    return json.dumps([a.model_dump() for a in items], separators=(",", ":"), ensure_ascii=False)


def load_addrs(raw: str | None) -> list[Address]:
    try:
        return [Address(**d) for d in json.loads(raw or "[]")]
    except Exception:  # noqa: BLE001 - a corrupt cell must not break a listing
        return []


class IndexStore:
    """All SQL for the index. Methods taking ``c`` run inside the caller's transaction."""

    def __init__(self, app: MailApp) -> None:
        self.app = app
        self.db: Database = app.db
        self.db.migrate()  # idempotent; picks up this package's migrations if imported late

    # ------------------------------------------------------------ capabilities
    @property
    def fts_available(self) -> bool:
        rows = self.db.query("SELECT 1 FROM sqlite_master WHERE name='idx_fts'")
        return bool(rows)

    # ------------------------------------------------------------ sync state
    def mailbox_state(self, account: str, mailbox: str) -> sqlite3.Row | None:
        rows = self.db.query("SELECT * FROM idx_mailboxes WHERE account=? AND mailbox=?",
                             (account, mailbox))
        return rows[0] if rows else None

    def indexed_flags(self, account: str, mailbox: str) -> dict[int, list[str]]:
        rows = self.db.query(
            "SELECT uid, flags_json FROM idx_messages WHERE account=? AND mailbox=?",
            (account, mailbox))
        return {int(r["uid"]): json.loads(r["flags_json"]) for r in rows}

    def freshness(self, account: str, mailboxes: list[str]) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for mb in mailboxes:
            st = self.mailbox_state(account, mb)
            out[mb] = st["last_sync_at"] if st else None
        return out

    def event_seq(self, account: str) -> int:
        rows = self.db.query("SELECT event_seq FROM idx_accounts WHERE account=?", (account,))
        return int(rows[0][0]) if rows else 0

    @staticmethod
    def set_event_seq(c: sqlite3.Connection, account: str, seq: int) -> None:
        c.execute(
            "INSERT INTO idx_accounts(account, event_seq, last_sync_at) VALUES (?,?,?) "
            "ON CONFLICT(account) DO UPDATE SET event_seq=excluded.event_seq, "
            "last_sync_at=excluded.last_sync_at", (account, seq, now_iso()))

    @staticmethod
    def touch_mailbox(c: sqlite3.Connection, account: str, mailbox: str,
                      uidvalidity: int) -> None:
        c.execute(
            "INSERT INTO idx_mailboxes(account, mailbox, uidvalidity, last_sync_at) "
            "VALUES (?,?,?,?) ON CONFLICT(account, mailbox) DO UPDATE SET "
            "uidvalidity=excluded.uidvalidity, last_sync_at=excluded.last_sync_at",
            (account, mailbox, uidvalidity, now_iso()))

    # ------------------------------------------------------------ writes
    @staticmethod
    def upsert_message(c: sqlite3.Connection, account: str, uidvalidity: int,
                       s: MessageSummary) -> int:
        sender = s.from_[0].email.lower() if s.from_ else None
        flags = list(s.flags)
        seen = int(any(f.lower() == "\\seen" for f in flags))
        c.execute(
            "INSERT INTO idx_messages(account, mailbox, uidvalidity, uid, message_id, date, "
            "internal_date, subject, from_json, to_json, cc_json, sender, flags_json, seen, "
            "size, has_attachments, indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account, mailbox, uidvalidity, uid) DO UPDATE SET "
            "message_id=excluded.message_id, date=excluded.date, "
            "internal_date=excluded.internal_date, subject=excluded.subject, "
            "from_json=excluded.from_json, to_json=excluded.to_json, cc_json=excluded.cc_json, "
            "sender=excluded.sender, flags_json=excluded.flags_json, seen=excluded.seen, "
            "size=excluded.size, has_attachments=excluded.has_attachments",
            (account, s.mailbox, uidvalidity, s.uid, s.message_id,
             s.date.isoformat() if s.date else None,
             s.internal_date.isoformat() if s.internal_date else None, s.subject or "",
             _addrs(s.from_), _addrs(s.to), _addrs(s.cc), sender, json.dumps(flags), seen,
             s.size, None if s.has_attachments is None else int(s.has_attachments), now_iso()))
        row = c.execute(
            "SELECT id FROM idx_messages WHERE account=? AND mailbox=? AND uidvalidity=? "
            "AND uid=?", (account, s.mailbox, uidvalidity, s.uid)).fetchone()
        return int(row[0])

    def write_fts(self, c: sqlite3.Connection, row_id: int, s: MessageSummary,
                  body: str = "", attachments: str = "") -> None:
        if not self.fts_available:
            return
        addrs = " ".join(f"{a.name or ''} {a.email}" for a in [*s.from_, *s.to, *s.cc])
        c.execute("DELETE FROM idx_fts WHERE rowid=?", (row_id,))
        c.execute("INSERT INTO idx_fts(rowid, subject, addrs, body, attachments) "
                  "VALUES (?,?,?,?,?)", (row_id, s.subject or "", addrs, body, attachments))
        c.execute("UPDATE idx_messages SET body_indexed=? WHERE id=?",
                  (int(bool(body or attachments)), row_id))

    @staticmethod
    def update_flags(c: sqlite3.Connection, account: str, mailbox: str,
                     changes: dict[int, list[str]]) -> None:
        for uid, flags in changes.items():
            seen = int(any(f.lower() == "\\seen" for f in flags))
            c.execute("UPDATE idx_messages SET flags_json=?, seen=? WHERE account=? AND "
                      "mailbox=? AND uid=?", (json.dumps(flags), seen, account, mailbox, uid))

    @staticmethod
    def delete_uids(c: sqlite3.Connection, account: str, mailbox: str,
                    uids: Iterable[int]) -> int:
        n = 0
        for uid in uids:
            n += c.execute("DELETE FROM idx_messages WHERE account=? AND mailbox=? AND uid=?",
                           (account, mailbox, uid)).rowcount
        return n

    @staticmethod
    def wipe_mailbox(c: sqlite3.Connection, account: str, mailbox: str) -> int:
        n = c.execute("DELETE FROM idx_messages WHERE account=? AND mailbox=?",
                      (account, mailbox)).rowcount
        c.execute("DELETE FROM idx_mailboxes WHERE account=? AND mailbox=?", (account, mailbox))
        return n

    def strip_bodies(self) -> int:
        """Downgrade INDEX -> METADATA: drop body and attachment text, keep headers."""
        if not self.fts_available:
            return 0
        with self.db.tx() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM idx_messages WHERE body_indexed=1")]
            for i in ids:
                c.execute("UPDATE idx_fts SET body='', attachments='' WHERE rowid=?", (i,))
            c.execute("UPDATE idx_messages SET body_indexed=0 WHERE body_indexed=1")
        return len(ids)

    # ------------------------------------------------------------ reads
    def count(self, table: str, account: str | None = None) -> int:
        if table not in TABLES:
            raise ValueError(table)
        if account is None:
            return int(self.db.query(f"SELECT COUNT(*) FROM {table}")[0][0])
        return int(self.db.query(f"SELECT COUNT(*) FROM {table} WHERE account=?",
                                 (account,))[0][0])

    # ------------------------------------------------------------ purge / export
    def purge(self, account: str | None = None, older_than: datetime | None = None,
              include_saved_searches: bool = False) -> dict[str, int]:
        """Delete cached rows (all, one account, or those cached before ``older_than``).

        Sync cursors of purged scope are dropped too so the next sync starts from scratch.
        """
        conn = self.db._conn  # noqa: SLF001 - PRAGMAs need the raw connection, outside a tx
        with self.db._lock:  # noqa: SLF001
            previous = conn.execute("PRAGMA secure_delete").fetchone()[0]
            conn.execute("PRAGMA secure_delete=ON")
            try:
                counts = self._purge_rows(account, older_than, include_saved_searches)
                if self.fts_available:
                    conn.execute("INSERT INTO idx_fts(idx_fts) VALUES('optimize')")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
            finally:
                conn.execute(f"PRAGMA secure_delete={int(previous)}")
        return counts

    def _purge_rows(self, account: str | None, older_than: datetime | None,
                    include_saved: bool) -> dict[str, int]:
        where, params = "1=1", []
        if account is not None:
            where += " AND account=?"
            params.append(account)
        msg_where, msg_params = where, list(params)
        if older_than is not None:
            if older_than.tzinfo is None:
                older_than = older_than.replace(tzinfo=UTC)
            msg_where += " AND indexed_at < ?"
            msg_params.append(older_than.astimezone(UTC).isoformat())
        with self.db.tx() as c:
            messages = c.execute(f"DELETE FROM idx_messages WHERE {msg_where}",
                                 msg_params).rowcount
            if older_than is None:
                c.execute(f"DELETE FROM idx_mailboxes WHERE {where}", params)
                c.execute(f"DELETE FROM idx_accounts WHERE {where}", params)
            else:  # partial purge: force the next sync to re-verify what remains
                c.execute(f"UPDATE idx_mailboxes SET last_sync_at=NULL WHERE {where}", params)
            saved = 0
            if include_saved:
                saved = c.execute(f"DELETE FROM saved_searches WHERE {where}", params).rowcount
        return {"messages": messages, "saved_searches": saved}

    def export_rows(self) -> Iterable[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM idx_messages ORDER BY account, mailbox, uid")
        for r in rows:
            yield {
                "account": r["account"], "mailbox": r["mailbox"],
                "uidvalidity": r["uidvalidity"], "uid": r["uid"],
                "message_id": r["message_id"], "date": r["date"],
                "internal_date": r["internal_date"], "subject": r["subject"],
                "from": json.loads(r["from_json"]), "to": json.loads(r["to_json"]),
                "cc": json.loads(r["cc_json"]), "flags": json.loads(r["flags_json"]),
                "size": r["size"], "has_attachments": r["has_attachments"],
                "indexed_at": r["indexed_at"],
            }


# ---------------------------------------------------------------- owner controls


def purge(app: MailApp, owner: CallerContext, account: str | None = None, *,
          older_than: datetime | None = None, include_saved_searches: bool = False
          ) -> dict[str, int]:
    """Owner-only. Delete retained messages (``account=None`` means every account).

    Works in any storage mode, so data left behind after switching to ``live`` can
    be removed. Uses ``secure_delete`` and compacts the database file.
    """
    _require_owner(owner)
    return IndexStore(app).purge(account, older_than, include_saved_searches)


def export_index(app: MailApp, owner: CallerContext, path: str | Path) -> int:
    """Owner-only. Write cached *metadata* as JSON lines (never bodies). Returns the row count."""
    _require_owner(owner)
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for row in IndexStore(app).export_rows():
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            n += 1
    return n


def _dir_size(path: Path) -> int:
    total = 0
    if path.is_dir():
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    return total


def _db_file_size(app: MailApp) -> int:
    if app.db.path == ":memory:":
        return 0
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += Path(app.db.path + suffix).stat().st_size
        except OSError:
            continue
    return total


def storage_report(app: MailApp, caller: CallerContext | None = None) -> dict[str, Any]:
    """Row counts, file sizes and oldest records.

    The owner (or ``caller=None``, for CLI/UI use) sees everything. Any other caller
    sees only per-account index counts for accounts it may read, with no paths or sizes.
    """
    store = IndexStore(app)
    mode = app.config.storage_mode
    owner_view = caller is None or caller.is_owner
    accounts = [a.name for a in app.config.accounts]
    if not owner_view:
        assert caller is not None
        readable = []
        for a in accounts:
            try:
                app.authorize_read(caller, a, kind="storage.report")
            except MailError:
                continue
            readable.append(a)
        accounts = readable
    per_account = {}
    for a in accounts:
        oldest = store.db.query(
            "SELECT MIN(indexed_at), MAX(indexed_at) FROM idx_messages WHERE account=?", (a,))[0]
        per_account[a] = {"messages": store.count("idx_messages", a),
                          "body_indexed": int(store.db.query(
                              "SELECT COUNT(*) FROM idx_messages WHERE account=? AND "
                              "body_indexed=1", (a,))[0][0]),
                          "saved_searches": store.count("saved_searches", a),
                          "oldest_record": oldest[0], "newest_record": oldest[1]}
    report: dict[str, Any] = {
        "storage_mode": mode.value,
        "encrypted": False,
        "encryption_note": "Encryption at rest is not implemented; rely on OS disk encryption "
                           "and file permissions. See index/store.py.",
        "fts_available": store.fts_available,
        "accounts": per_account,
    }
    if mode is StorageMode.LIVE and store.count("idx_messages"):
        report["warning"] = ("storage_mode is live but cached messages remain from an earlier "
                             "mode; purge them to remove the data")
    if not owner_view:
        return report
    oldest_op = app.db.query("SELECT MIN(created_at) FROM operations")[0][0]
    report.update({
        "rows": {t: store.count(t) for t in TABLES},
        "operational_rows": {
            "operations": int(app.db.query("SELECT COUNT(*) FROM operations")[0][0]),
            "events": int(app.db.query("SELECT COUNT(*) FROM events")[0][0]),
            "artifacts": int(app.db.query("SELECT COUNT(*) FROM artifacts")[0][0]),
        },
        "oldest_operation": oldest_op,
        "database_path": app.db.path,
        "database_bytes": _db_file_size(app),
        "artifacts_path": str(app.config.resolved_artifact_dir()),
        "artifacts_bytes": _dir_size(app.config.resolved_artifact_dir()),
        "retention_days": app.config.retention_days,
        "retention_note": "retention_days governs operational records (journal, events); "
                          "cached messages are removed with purge().",
    })
    return report
