"""Change tracking: reconciliation, the event journal, and the background watcher.

Bridge exposes no incremental flag synchronization (no CONDSTORE/QRESYNC), so
changes are found by comparing the current UID -> flags map of a mailbox with a
snapshot stored in ``mailbox_state``. The differences become journal events:

* ``message_added`` / ``message_removed`` / ``flags_changed``
* ``label_membership_changed`` (same diff, but for Proton label mailboxes where
  a UID appearing or disappearing is a membership change, not new mail)
* ``mailbox_reset`` (UIDVALIDITY changed: full resync, handles and pending
  mutation targets for that mailbox are stale)
* ``mailbox_created`` / ``mailbox_deleted``

Event data carries opaque handle tokens, UIDs and flags only: never subjects,
addresses or bodies. Notifications are hints; clients catch up with a cursor.

The first reconciliation of a mailbox records a baseline and emits nothing.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import weakref
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..domain.errors import ErrorCode, MailError, invalid
from ..domain.families import OperationFamily, register_kind
from ..domain.models import MailboxRole, MessageHandle, Model
from ..domain.requests import CallerContext
from .core import MailApp

log = logging.getLogger(__name__)

KIND_LIST = register_kind("events.list", OperationFamily.READ)
KIND_RECONCILE = register_kind("events.reconcile", OperationFamily.READ)

EVENT_TYPES = frozenset({
    "message_added", "message_removed", "flags_changed", "mailbox_reset",
    "mailbox_created", "mailbox_deleted", "label_membership_changed",
})

MAX_SCAN_UIDS = 50_000  # newest UIDs compared per mailbox; older ones are not tracked
MAX_PAGE = 1000
_MAILBOX_LIST_KEY = "\x00mailboxes"  # sentinel mailbox_state row: the known mailbox names
_IGNORED_FLAGS = frozenset({"\\recent"})


def _now() -> datetime:
    return datetime.now(UTC)


def _norm_flags(flags: Iterable[str]) -> list[str]:
    return sorted({f for f in flags if f.lower() not in _IGNORED_FLAGS}, key=str.lower)


# ---------------------------------------------------------------- journal


class EventPage(Model):
    events: list[dict[str, Any]]
    next_cursor: int  # pass back as ``cursor`` to receive only newer events
    has_more: bool = False


class EventStore:
    """Append-only journal over the ``events`` table."""

    def __init__(self, app: MailApp) -> None:
        self._db = app.db

    def append(self, account: str, mailbox: str | None, type: str, data: dict[str, Any]) -> int:  # noqa: A002
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {type!r}")
        with self._db.tx() as c:
            return self._insert(c, account, mailbox, type, data)

    @staticmethod
    def _insert(c: Any, account: str, mailbox: str | None, type: str,  # noqa: A002
                data: dict[str, Any]) -> int:
        cur = c.execute(
            "INSERT INTO events(account, mailbox, type, data_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (account, mailbox, type, json.dumps(data, separators=(",", ":")),
             _now().isoformat()))
        return int(cur.lastrowid)

    def list(self, account: str, after_seq: int = 0, limit: int = 100,
             types: Iterable[str] | None = None) -> EventPage:
        limit = max(1, min(int(limit), MAX_PAGE))
        wanted = sorted(set(types)) if types else []
        unknown = [t for t in wanted if t not in EVENT_TYPES]
        if unknown:
            raise invalid("unknown event type", types=unknown)
        sql = "SELECT * FROM events WHERE account=? AND seq>?"
        params: list[Any] = [account, int(after_seq)]
        if wanted:
            sql += f" AND type IN ({','.join('?' * len(wanted))})"
            params += wanted
        sql += " ORDER BY seq LIMIT ?"
        params.append(limit + 1)
        rows = self._db.query(sql, tuple(params))
        has_more = len(rows) > limit
        rows = rows[:limit]
        events = [self._row(r) for r in rows]
        next_cursor = int(rows[-1]["seq"]) if rows else int(after_seq)
        return EventPage(events=events, next_cursor=next_cursor, has_more=has_more)

    @staticmethod
    def _row(r: Any) -> dict[str, Any]:
        return {"seq": r["seq"], "account": r["account"], "mailbox": r["mailbox"],
                "type": r["type"], "data": json.loads(r["data_json"]),
                "created_at": r["created_at"]}

    def purge(self, older_than: datetime) -> int:
        if older_than.tzinfo is None:
            older_than = older_than.replace(tzinfo=UTC)
        with self._db.tx() as c:
            return c.execute("DELETE FROM events WHERE created_at < ?",
                             (older_than.astimezone(UTC).isoformat(),)).rowcount


def purge_events(app: MailApp, older_than: datetime) -> int:
    """Retention: delete journal entries older than ``older_than``. Returns the count."""
    return EventStore(app).purge(older_than)


# ---------------------------------------------------------------- reconciliation


@dataclass
class ReconcileReport:
    account: str
    mailbox: str
    baseline: bool = False
    reset: bool = False
    added: int = 0
    removed: int = 0
    flags_changed: int = 0
    events: int = 0
    scanned: int = 0
    truncated: bool = False  # more than MAX_SCAN_UIDS messages; only the newest were compared
    scan_ms: int = 0
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class _State:
    uidvalidity: int | None
    flags: dict[int, list[str]]
    last_reconciled_at: str | None = None
    scan_ms: int | None = None
    uidnext: int | None = None


class Reconciler:
    """Compares live mailbox state with the stored snapshot and journals the differences."""

    def __init__(self, app: MailApp, max_scan: int = MAX_SCAN_UIDS) -> None:
        self.app = app
        self.events = EventStore(app)
        self.max_scan = max_scan
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------ scope
    def watch_scope(self, account: str) -> list[str]:
        """Configured watched mailboxes, plus label mailboxes when ``poll_labels``."""
        watch = self.app.config.watch
        scope = list(dict.fromkeys(watch.mailboxes))
        if watch.poll_labels:
            for mb in self.label_mailboxes(account):
                if mb not in scope:
                    scope.append(mb)
        return scope

    def label_mailboxes(self, account: str) -> list[str]:
        infos = self.app.store(account).list_mailboxes()
        return [i.name for i in infos if i.role is MailboxRole.LABEL and i.selectable]

    # ------------------------------------------------------------ state
    def _lock(self, account: str, mailbox: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault((account, mailbox), threading.Lock())

    def load_state(self, account: str, mailbox: str) -> _State | None:
        rows = self.app.db.query(
            "SELECT * FROM mailbox_state WHERE account=? AND mailbox=?", (account, mailbox))
        if not rows:
            return None
        r = rows[0]
        raw = json.loads(r["flags_json"]) if r["flags_json"] else {}
        return _State(r["uidvalidity"], {int(k): list(v) for k, v in raw.items()},
                      r["last_reconciled_at"], r["scan_ms"], r["uidnext"])

    @staticmethod
    def _save_state(c: Any, account: str, mailbox: str, uidvalidity: int | None,
                    flags: dict[int, list[str]], scan_ms: int) -> None:
        uidnext = (max(flags) + 1) if flags else None
        c.execute(
            "INSERT INTO mailbox_state(account, mailbox, uidvalidity, uidnext, flags_json, "
            "last_reconciled_at, scan_ms) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(account, mailbox) DO UPDATE SET uidvalidity=excluded.uidvalidity, "
            "uidnext=excluded.uidnext, flags_json=excluded.flags_json, "
            "last_reconciled_at=excluded.last_reconciled_at, scan_ms=excluded.scan_ms",
            (account, mailbox, uidvalidity, uidnext,
             json.dumps({str(k): v for k, v in flags.items()}, separators=(",", ":")),
             _now().isoformat(), scan_ms))

    # ------------------------------------------------------------ scan
    def _scan(self, account: str, mailbox: str) -> tuple[int, dict[int, list[str]], bool]:
        store = self.app.store(account)
        for attempt in (0, 1):
            uidvalidity, uids = store.list_uids(mailbox)
            truncated = len(uids) > self.max_scan
            if truncated:
                uids = uids[-self.max_scan:]
            try:
                flags = store.fetch_flags(mailbox, uidvalidity, uids)
            except MailError as exc:
                if exc.code is ErrorCode.STALE_HANDLE and attempt == 0:
                    continue  # UIDVALIDITY changed between the two calls: rescan once
                raise
            return uidvalidity, {u: _norm_flags(f) for u, f in flags.items()}, truncated
        raise MailError(ErrorCode.INTERNAL, "mailbox kept changing during scan")  # pragma: no cover

    def reconcile(self, account: str, mailbox: str) -> ReconcileReport:
        report = ReconcileReport(account, mailbox)
        with self._lock(account, mailbox):
            started = time.monotonic()
            try:
                uidvalidity, current, truncated = self._scan(account, mailbox)
            except MailError as exc:
                if exc.code is ErrorCode.NOT_FOUND:
                    return self._mailbox_gone(account, mailbox, report)
                raise
            report.scanned = len(current)
            report.truncated = truncated
            report.scan_ms = int((time.monotonic() - started) * 1000)
            previous = self.load_state(account, mailbox)
            is_label = self.app.store(account).role_of(mailbox) is MailboxRole.LABEL
            with self.app.db.tx() as c:
                if previous is None or previous.uidvalidity is None:
                    report.baseline = True
                elif previous.uidvalidity != uidvalidity:
                    report.reset = True
                    self._emit(c, report, "mailbox_reset", {
                        "previous_uidvalidity": previous.uidvalidity,
                        "uidvalidity": uidvalidity, "messages": len(current)})
                else:
                    self._diff(c, report, previous, current, uidvalidity, truncated, is_label)
                self._save_state(c, account, mailbox, uidvalidity, current, report.scan_ms)
        return report

    def _diff(self, c: Any, report: ReconcileReport, previous: _State,
              current: dict[int, list[str]], uidvalidity: int, truncated: bool,
              is_label: bool) -> None:
        old = previous.flags
        if truncated and current:
            floor = min(current)  # UIDs below the scan window are not tracked
            old = {u: f for u, f in old.items() if u >= floor}

        def handle(uid: int) -> str:
            return MessageHandle(account=report.account, mailbox=report.mailbox,
                                 uidvalidity=uidvalidity, uid=uid).token()

        for uid in sorted(set(current) - set(old)):
            if is_label:
                self._emit(c, report, "label_membership_changed",
                           {"handle": handle(uid), "uid": uid, "change": "added",
                            "flags": current[uid]})
            else:
                self._emit(c, report, "message_added",
                           {"handle": handle(uid), "uid": uid, "flags": current[uid]})
            report.added += 1
        for uid in sorted(set(old) - set(current)):
            if is_label:
                self._emit(c, report, "label_membership_changed",
                           {"handle": handle(uid), "uid": uid, "change": "removed"})
            else:
                self._emit(c, report, "message_removed", {"handle": handle(uid), "uid": uid})
            report.removed += 1
        if is_label:
            return  # label mailboxes track membership only
        for uid in sorted(set(current) & set(old)):
            before, after = old[uid], current[uid]
            if {f.lower() for f in before} != {f.lower() for f in after}:
                lb = {f.lower() for f in before}
                la = {f.lower() for f in after}
                self._emit(c, report, "flags_changed", {
                    "handle": handle(uid), "uid": uid, "flags": after,
                    "added": [f for f in after if f.lower() not in lb],
                    "removed": [f for f in before if f.lower() not in la]})
                report.flags_changed += 1

    def _emit(self, c: Any, report: ReconcileReport, type: str,  # noqa: A002
              data: dict[str, Any]) -> None:
        self.events._insert(c, report.account, report.mailbox, type, data)
        report.events += 1

    def _mailbox_gone(self, account: str, mailbox: str, report: ReconcileReport
                      ) -> ReconcileReport:
        """The mailbox no longer exists: journal the deletion once and drop its snapshot."""
        if self.load_state(account, mailbox) is not None:
            with self.app.db.tx() as c:
                self._emit(c, report, "mailbox_deleted", {"mailbox": mailbox})
                c.execute("DELETE FROM mailbox_state WHERE account=? AND mailbox=?",
                          (account, mailbox))
        else:
            report.error = {"code": ErrorCode.NOT_FOUND.value, "message": "mailbox not found"}
        return report

    # ------------------------------------------------------------ mailbox list
    def reconcile_mailbox_list(self, account: str) -> int:
        """Emit mailbox_created/mailbox_deleted by diffing the mailbox names. Returns events."""
        names = sorted(i.name for i in self.app.store(account).list_mailboxes() if i.selectable)
        with self._lock(account, _MAILBOX_LIST_KEY):
            rows = self.app.db.query(
                "SELECT flags_json FROM mailbox_state WHERE account=? AND mailbox=?",
                (account, _MAILBOX_LIST_KEY))
            known = set(json.loads(rows[0]["flags_json"])) if rows else None
            emitted = 0
            with self.app.db.tx() as c:
                if known is not None:  # first run is a baseline: no events
                    for name in sorted(set(names) - known):
                        self.events._insert(c, account, name, "mailbox_created", {"mailbox": name})
                        emitted += 1
                    for name in sorted(known - set(names)):
                        self.events._insert(c, account, name, "mailbox_deleted", {"mailbox": name})
                        c.execute("DELETE FROM mailbox_state WHERE account=? AND mailbox=?",
                                  (account, name))
                        emitted += 1
                c.execute(
                    "INSERT INTO mailbox_state(account, mailbox, flags_json, last_reconciled_at) "
                    "VALUES (?,?,?,?) ON CONFLICT(account, mailbox) DO UPDATE SET "
                    "flags_json=excluded.flags_json, "
                    "last_reconciled_at=excluded.last_reconciled_at",
                    (account, _MAILBOX_LIST_KEY, json.dumps(names), _now().isoformat()))
        return emitted

    def reconcile_all(self, account: str) -> list[ReconcileReport]:
        """Reconcile the whole watch scope; one failing mailbox does not stop the others."""
        reports: list[ReconcileReport] = []
        try:
            self.reconcile_mailbox_list(account)
        except MailError as exc:
            log.warning("mailbox list reconciliation failed: %s", exc.code)
        for mailbox in self.watch_scope(account):
            reports.append(self.reconcile_safely(account, mailbox))
        return reports

    def reconcile_safely(self, account: str, mailbox: str) -> ReconcileReport:
        try:
            return self.reconcile(account, mailbox)
        except MailError as exc:
            return ReconcileReport(account, mailbox, error=exc.to_dict())


# ---------------------------------------------------------------- watcher


_WATCHERS: weakref.WeakKeyDictionary[MailApp, Watcher] = weakref.WeakKeyDictionary()


def watcher_for(app: MailApp) -> Watcher | None:
    return _WATCHERS.get(app)


@dataclass
class _AccountWatch:
    idle_mailbox: str | None = None
    idle_active: bool = False
    idle_unavailable: bool = False
    last_error: str | None = None
    threads: list[threading.Thread] = field(default_factory=list)


class Watcher:
    """Background change detection with at most one IDLE connection per account.

    Per account: one IDLE loop on the first watched mailbox (triggers a
    reconciliation on activity) and one polling loop covering the remaining
    scope every ``poll_interval`` seconds. Errors (Bridge restarts, network)
    are caught and retried with exponential backoff. If IDLE is unavailable or
    disabled the polling loop covers every mailbox.
    """

    def __init__(self, app: MailApp, *, poll_interval: float | None = None,
                 idle_slice: float = 15.0, backoff_initial: float = 1.0,
                 backoff_cap: float = 60.0, reconciler: Reconciler | None = None) -> None:
        self.app = app
        self.reconciler = reconciler or Reconciler(app)
        self.poll_interval = (float(poll_interval) if poll_interval is not None
                              else float(app.config.watch.poll_interval_seconds))
        self.idle_slice = idle_slice
        self.backoff_initial = backoff_initial
        self.backoff_cap = backoff_cap
        self._stop = threading.Event()
        self._accounts: dict[str, _AccountWatch] = {}
        self._running = False
        _WATCHERS[app] = self

    # ------------------------------------------------------------ lifecycle
    @property
    def running(self) -> bool:
        return self._running and not self._stop.is_set()

    def start(self) -> None:
        if self._running:
            return
        self._stop.clear()
        self._running = True
        for acct in self.app.config.accounts:
            state = _AccountWatch()
            scope = self.app.config.watch.mailboxes
            if self.app.config.watch.idle and scope:
                state.idle_mailbox = scope[0]
            self._accounts[acct.name] = state
            loops = [("poll", self._poll_loop)]
            if state.idle_mailbox:
                loops.append(("idle", self._idle_loop))
            for kind, fn in loops:
                t = threading.Thread(target=fn, args=(acct.name, state), daemon=True,
                                     name=f"mcp-proton-{kind}-{acct.name}")
                state.threads.append(t)
                t.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal every loop to exit and wait for them (bounded by ``timeout`` in total)."""
        self._stop.set()
        deadline = time.monotonic() + timeout
        for state in self._accounts.values():
            for t in state.threads:
                t.join(max(0.0, deadline - time.monotonic()))
        self._running = False

    def idle_active(self, account: str) -> bool:
        st = self._accounts.get(account)
        return bool(st and st.idle_active and not self._stop.is_set())

    def idle_mailbox(self, account: str) -> str | None:
        st = self._accounts.get(account)
        return st.idle_mailbox if st and not st.idle_unavailable else None

    def last_error(self, account: str) -> str | None:
        st = self._accounts.get(account)
        return st.last_error if st else None

    # ------------------------------------------------------------ loops
    def _backoff(self, attempt: int) -> float:
        return min(self.backoff_cap, self.backoff_initial * (2 ** attempt))

    def _poll_loop(self, account: str, state: _AccountWatch) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                self._poll_once(account, state)
                failures = 0
                state.last_error = None
                delay = self.poll_interval
            except MailError as exc:
                state.last_error = exc.code.value
                delay = max(self._backoff(failures), 0.0)
                failures += 1
            except Exception:  # noqa: BLE001 - a watcher must never die
                log.exception("poll loop error")
                state.last_error = ErrorCode.INTERNAL.value
                delay = self._backoff(failures)
                failures += 1
            self._stop.wait(delay)

    def _poll_once(self, account: str, state: _AccountWatch) -> None:
        self.reconciler.reconcile_mailbox_list(account)
        idle_mb = state.idle_mailbox if state.idle_active else None
        first_error: MailError | None = None
        for mailbox in self.reconciler.watch_scope(account):
            if self._stop.is_set():
                return
            if mailbox == idle_mb:
                continue  # the IDLE loop covers it
            report = self.reconciler.reconcile_safely(account, mailbox)
            if report.error and first_error is None and report.error["code"] != "not_found":
                first_error = MailError(ErrorCode(report.error["code"]), "reconcile failed")
        if first_error is not None:
            raise first_error

    def _marker(self, account: str, mailbox: str) -> tuple[int | None, int | None, int | None]:
        info = self.app.store(account).mailbox_status(mailbox)
        return (info.uidnext, info.messages, info.uidvalidity)

    def _idle_loop(self, account: str, state: _AccountWatch) -> None:
        mailbox = state.idle_mailbox
        assert mailbox is not None
        failures = 0
        synced = False
        last_marker: tuple[int | None, int | None, int | None] | None = None
        while not self._stop.is_set() and not state.idle_unavailable:
            try:
                if not synced:
                    # Events between IDLE sessions are not replayed: always re-sync first.
                    # STATUS first: anything arriving after it makes the marker differ later.
                    last_marker = self._marker(account, mailbox)
                    self.reconciler.reconcile_safely(account, mailbox)
                    synced = True
                state.idle_active = True
                store = self.app.store(account)
                responses = store.idle_wait(mailbox, self.idle_slice)
                failures = 0
                state.last_error = None
                if self._stop.is_set():
                    break
                # Changes between two IDLE sessions are not replayed, so an empty return
                # still compares the cheap STATUS counters with the last seen ones.
                marker = self._marker(account, mailbox)
                if responses or marker != last_marker:
                    self.reconciler.reconcile(account, mailbox)
                last_marker = marker
            except MailError as exc:
                state.idle_active = False
                if exc.code is ErrorCode.CAPABILITY_UNAVAILABLE:
                    state.idle_unavailable = True  # polling-only from here on
                    state.last_error = exc.code.value
                    return
                state.last_error = exc.code.value
                synced = False
                self._stop.wait(self._backoff(failures))
                failures += 1
            except Exception:  # noqa: BLE001
                log.exception("idle loop error")
                state.idle_active = False
                state.last_error = ErrorCode.INTERNAL.value
                synced = False
                self._stop.wait(self._backoff(failures))
                failures += 1
        state.idle_active = False


# ---------------------------------------------------------------- read API


def _parse_cursor(cursor: int | str | None) -> int:
    if cursor is None or cursor == "":
        return 0
    try:
        value = int(cursor)
    except (TypeError, ValueError):
        raise invalid("malformed cursor") from None
    if value < 0:
        raise invalid("malformed cursor")
    return value


def list_events(app: MailApp, caller: CallerContext, account: str,
                cursor: int | str | None = None, limit: int = 100,
                types: list[str] | None = None) -> EventPage:
    """Events after ``cursor`` (the last seq the client saw). Events for mailboxes the
    caller may not read are omitted but still advance the cursor."""
    app.authorize_read(caller, account, kind=KIND_LIST)
    page = EventStore(app).list(account, _parse_cursor(cursor), limit, types)
    readable: dict[str | None, bool] = {}

    def allowed(mailbox: str | None) -> bool:
        if mailbox not in readable:
            try:
                app.authorize_read(caller, account, [mailbox] if mailbox else None,
                                   kind=KIND_LIST)
                readable[mailbox] = True
            except MailError:
                readable[mailbox] = False
        return readable[mailbox]

    page.events = [e for e in page.events if allowed(e["mailbox"])]
    return page


def reconcile_now(app: MailApp, caller: CallerContext, account: str,
                  mailbox: str | None = None) -> dict[str, Any]:
    """Reconcile one mailbox (or the whole watch scope) immediately.

    Only reads mail state and writes our own journal, hence a read-kind."""
    app.authorize_read(caller, account, [mailbox] if mailbox else None, kind=KIND_RECONCILE)
    rec = _reconciler(app)
    reports = [rec.reconcile(account, mailbox)] if mailbox else rec.reconcile_all(account)
    return {"account": account, "reports": [r.to_dict() for r in reports],
            "events": sum(r.events for r in reports)}


def _reconciler(app: MailApp) -> Reconciler:
    w = watcher_for(app)
    return w.reconciler if w is not None else Reconciler(app)


def watch_status(app: MailApp, caller: CallerContext, account: str) -> dict[str, Any]:
    """Watch scope, polling interval, per-mailbox last reconciliation, scan cost, freshness."""
    app.authorize_read(caller, account, kind=KIND_LIST)
    rec = _reconciler(app)
    watch = app.config.watch
    watcher = watcher_for(app)
    try:
        scope = rec.watch_scope(account)
    except MailError:
        scope = list(watch.mailboxes)  # Bridge unreachable: report the configured scope
    now = _now()
    mailboxes = []
    for mb in scope:
        st = rec.load_state(account, mb)
        fresh: float | None = None
        if st and st.last_reconciled_at:
            fresh = round((now - datetime.fromisoformat(st.last_reconciled_at)).total_seconds(), 3)
        mailboxes.append({
            "mailbox": mb,
            "last_reconciled_at": st.last_reconciled_at if st else None,
            "scan_ms": st.scan_ms if st else None,
            "tracked_messages": len(st.flags) if st else 0,
            "freshness_seconds": fresh,
        })
    return {
        "account": account,
        "watch_mailboxes": list(watch.mailboxes),
        "poll_labels": watch.poll_labels,
        "scope": scope,
        "poll_interval_seconds": watcher.poll_interval if watcher else watch.poll_interval_seconds,
        "idle_configured": watch.idle,
        "idle_mailbox": watcher.idle_mailbox(account) if watcher else None,
        "idle_active": watcher.idle_active(account) if watcher else False,
        "watcher_running": bool(watcher and watcher.running),
        "last_error": watcher.last_error(account) if watcher else None,
        "mailboxes": mailboxes,
        "note": "Notifications are hints; use list_events with a cursor to catch up.",
    }


__all__ = [
    "EVENT_TYPES", "EventPage", "EventStore", "ReconcileReport", "Reconciler", "Watcher",
    "list_events", "purge_events", "reconcile_now", "watch_status", "watcher_for",
]
