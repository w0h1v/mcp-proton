"""Ordered schema migrations. Append only."""

from __future__ import annotations

import sqlite3

from .db import register_migration


def run_script(c: sqlite3.Connection, script: str) -> None:
    """Execute statements one by one (``executescript`` would commit the open transaction)."""
    script = "\n".join(line.split("--", 1)[0] for line in script.splitlines())
    for stmt in script.split(";"):
        if stmt.strip():
            c.execute(stmt)


def _m001_operations(c: sqlite3.Connection) -> None:
    run_script(c, 
        """
        CREATE TABLE operations (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            family TEXT NOT NULL,
            client_id TEXT NOT NULL,
            transport TEXT NOT NULL,
            account TEXT NOT NULL,
            status TEXT NOT NULL,
            digest TEXT NOT NULL,
            request_json TEXT NOT NULL,      -- exact payload; needed to resume approvals
            summary TEXT NOT NULL,
            idempotency_key TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT,
            decided_at TEXT,
            decided_by TEXT,
            started_at TEXT,
            finished_at TEXT,
            result_json TEXT,
            error_json TEXT,
            message_id TEXT,                 -- stable outgoing Message-ID for sends
            policy_reasons TEXT
        );
        CREATE INDEX operations_status ON operations(status);
        CREATE INDEX operations_client ON operations(client_id, created_at);
        CREATE UNIQUE INDEX operations_idem ON operations(client_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        CREATE TABLE operation_items (
            operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            target TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT,
            new_handle TEXT,
            prior_state TEXT,               -- JSON for best-effort undo
            PRIMARY KEY (operation_id, seq)
        );
        """
    )


def _m002_events(c: sqlite3.Connection) -> None:
    run_script(c, 
        """
        CREATE TABLE events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT NOT NULL,
            mailbox TEXT,
            type TEXT NOT NULL,
            data_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX events_account ON events(account, seq);
        CREATE TABLE mailbox_state (
            account TEXT NOT NULL,
            mailbox TEXT NOT NULL,
            uidvalidity INTEGER,
            uidnext INTEGER,
            flags_json TEXT,                -- {uid: [flags]} snapshot for reconciliation
            last_reconciled_at TEXT,
            scan_ms INTEGER,
            PRIMARY KEY (account, mailbox)
        );
        """
    )


def _m003_jobs(c: sqlite3.Connection) -> None:
    run_script(c, 
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,             -- scheduled_send | reminder | snooze | rule | webhook
            client_id TEXT NOT NULL,
            account TEXT NOT NULL,
            status TEXT NOT NULL,           -- active | paused | done | cancelled | failed
            spec_json TEXT NOT NULL,
            next_run_at TEXT,
            timezone TEXT,
            missed_run_policy TEXT NOT NULL DEFAULT 'run_once',
            last_run_at TEXT,
            last_result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX jobs_due ON jobs(status, next_run_at);
        CREATE TABLE job_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            operation_id TEXT,
            detail TEXT
        );
        CREATE TABLE artifacts (
            id TEXT PRIMARY KEY,
            account TEXT NOT NULL,
            source_handle TEXT,
            part_id TEXT,
            filename TEXT,
            content_type TEXT,
            size INTEGER,
            path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT
        );
        """
    )


register_migration("001_operations", _m001_operations)
register_migration("002_events", _m002_events)
register_migration("003_jobs", _m003_jobs)

# Optional feature schemas register from their own modules; import them here so
# every Database() gets the full schema regardless of import order.
from ..index import migrations as _index_migrations  # noqa: E402,F401
