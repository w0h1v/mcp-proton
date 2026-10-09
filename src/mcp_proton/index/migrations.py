"""Schema for the optional local metadata cache / full-text index (Phase 5).

The tables are created in every storage mode (they are empty in ``live`` mode
and nothing in this package writes to them then). Migrations are append-only
and registered through ``storage.db.register_migration``.

Tables:

* ``idx_messages``   one row per mailbox *occurrence* (account, mailbox, UIDVALIDITY, UID)
* ``idx_fts``        FTS5 over subject / addresses / body / attachment text, rowid = idx_messages.id
* ``idx_mailboxes``  per-mailbox sync state (UIDVALIDITY, last sync time)
* ``idx_accounts``   per-account journal cursor (last applied event sequence)
* ``saved_searches`` named searches (structured JSON or full-text)
"""

from __future__ import annotations

import logging
import sqlite3

from ..storage.db import register_migration
from ..storage.migrations import run_script

log = logging.getLogger(__name__)


def _m100_index_metadata(c: sqlite3.Connection) -> None:
    run_script(c, """
        CREATE TABLE idx_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT NOT NULL,
            mailbox TEXT NOT NULL,
            uidvalidity INTEGER NOT NULL,
            uid INTEGER NOT NULL,
            message_id TEXT,
            date TEXT,
            internal_date TEXT,
            subject TEXT,
            from_json TEXT NOT NULL,
            to_json TEXT NOT NULL,
            cc_json TEXT NOT NULL,
            sender TEXT,                     -- lower-cased first From address (grouping)
            flags_json TEXT NOT NULL,
            seen INTEGER NOT NULL DEFAULT 0,
            size INTEGER,
            has_attachments INTEGER,
            body_indexed INTEGER NOT NULL DEFAULT 0,
            indexed_at TEXT NOT NULL,
            UNIQUE (account, mailbox, uidvalidity, uid)
        );
        CREATE INDEX idx_messages_mailbox ON idx_messages(account, mailbox);
        CREATE INDEX idx_messages_sender ON idx_messages(account, sender);
        CREATE TABLE idx_mailboxes (
            account TEXT NOT NULL,
            mailbox TEXT NOT NULL,
            uidvalidity INTEGER,
            last_sync_at TEXT,
            PRIMARY KEY (account, mailbox)
        );
        CREATE TABLE idx_accounts (
            account TEXT PRIMARY KEY,
            event_seq INTEGER NOT NULL DEFAULT 0,
            last_sync_at TEXT
        );
        CREATE TABLE saved_searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT NOT NULL,
            client_id TEXT NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,              -- structured | text
            query TEXT NOT NULL,             -- SearchQuery JSON, or FTS text
            mailboxes_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (account, client_id, name)
        );
    """)


def _m101_index_fts(c: sqlite3.Connection) -> None:
    try:
        c.execute(
            "CREATE VIRTUAL TABLE idx_fts USING fts5(subject, addrs, body, attachments, "
            "tokenize='unicode61 remove_diacritics 2')")
    except sqlite3.OperationalError:  # SQLite built without FTS5
        log.warning("SQLite lacks FTS5; local full-text search is unavailable")
        return
    c.execute("CREATE TRIGGER idx_messages_ad AFTER DELETE ON idx_messages BEGIN "
              "DELETE FROM idx_fts WHERE rowid = old.id; END")


register_migration("100_index_metadata", _m100_index_metadata)
register_migration("101_index_fts", _m101_index_fts)
