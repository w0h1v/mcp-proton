"""Schema for the automation layer (registered through ``storage.db.register_migration``).

``jobs`` and ``job_runs`` come from the core migrations. This module adds:

* ``webhooks``      owner/AUTOMATION-registered notification destinations, each with its own
                    secret, delivery cursor and failure/backoff state
* ``job_cursors``   named event cursors (one per trigger rule)
* ``undo_log``      which operations were already undone (prevents a second undo)
"""

from __future__ import annotations

import sqlite3

from ..storage.db import register_migration
from ..storage.migrations import run_script


def _m200_jobs_automation(c: sqlite3.Connection) -> None:
    run_script(c, """
        CREATE TABLE webhooks (
            id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            account TEXT NOT NULL,
            url TEXT NOT NULL,
            secret TEXT NOT NULL,            -- HMAC key; revealed to the creator exactly once
            secret_revealed INTEGER NOT NULL DEFAULT 0,
            event_types_json TEXT NOT NULL,
            status TEXT NOT NULL,            -- active | paused | deleted
            cursor INTEGER NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            last_error TEXT,
            last_delivery_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX webhooks_account ON webhooks(account, status);
        CREATE TABLE job_cursors (
            name TEXT PRIMARY KEY,
            value INTEGER NOT NULL
        );
        CREATE TABLE undo_log (
            operation_id TEXT PRIMARY KEY,
            undo_operation_ids TEXT NOT NULL,
            client_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX job_runs_job ON job_runs(job_id, id);
    """)


register_migration("200_jobs_automation", _m200_jobs_automation)
