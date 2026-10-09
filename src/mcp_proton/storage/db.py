"""SQLite storage for operational records (journal, approvals, jobs, events).

Operational records exist even in live-access mode; their contents and
retention are documented in docs/storage.md. Schema migrations are linear and
recorded in ``schema_version``.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

Migration = Callable[[sqlite3.Connection], None]

# Modules append migrations in import order via ``register_migration``; the
# list index + 1 is the schema version. Never reorder or edit shipped entries.
_MIGRATIONS: list[tuple[str, Migration]] = []


def register_migration(name: str, fn: Migration) -> None:
    if any(n == name for n, _ in _MIGRATIONS):
        return
    _MIGRATIONS.append((name, fn))


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            Path(self.path).chmod(0o600)
        self.migrate()

    def migrate(self) -> None:
        # Import modules that register migrations so ordering is deterministic.
        from . import migrations  # noqa: F401

        with self.tx() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, "
                "applied_at TEXT DEFAULT CURRENT_TIMESTAMP)"
            )
            done = {r[0] for r in c.execute("SELECT name FROM schema_migrations")}
            for name, fn in _MIGRATIONS:
                if name not in done:
                    fn(c)
                    c.execute("INSERT INTO schema_migrations(name) VALUES (?)", (name,))

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serializable write transaction."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
