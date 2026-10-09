"""Keeps the local cache in step with the mailbox.

``Indexer.sync(account, mailbox)`` compares the server's UID list with the
cached one: new UIDs are fetched (summaries; plus body text in ``index`` mode),
vanished UIDs are deleted, flag changes are applied, and a UIDVALIDITY change
wipes the mailbox rows before a full re-sync (cached handles would be stale).

``Indexer.sync_from_events`` consumes the change-event journal written by the
reconciler/watcher and re-syncs only mailboxes that changed.

In ``live`` mode every entry point raises ``unsupported`` before touching the
database: this package retains nothing in that mode. Indexing is an owner-level
background operation (like the watcher); policy is enforced when results are
*read* (see ``search.py``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..bridge import mime
from ..config import StorageMode
from ..domain.errors import ErrorCode, MailError
from ..domain.models import MessageHandle, MessageSummary
from ..services.core import MailApp
from ..services.events import EventStore
from .extract import extract_text, supported
from .store import IndexStore, require_enabled

log = logging.getLogger(__name__)

CHUNK = 100
MAX_NEW_PER_SYNC = 5000
MAX_ATTACHMENT_PARTS = 10


@dataclass
class SyncReport:
    account: str
    mailbox: str
    added: int = 0
    removed: int = 0
    flags_updated: int = 0
    reset: bool = False
    truncated: bool = False
    body_failures: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class Indexer:
    def __init__(self, app: MailApp, *, index_attachments: bool = False,
                 max_new_per_sync: int = MAX_NEW_PER_SYNC) -> None:
        self.app = app
        self.store = IndexStore(app)
        self.index_attachments = index_attachments
        self.max_new = max_new_per_sync

    # ------------------------------------------------------------ sync
    def sync(self, account: str, mailbox: str) -> SyncReport:
        mode = require_enabled(self.app)
        if mode is StorageMode.METADATA:
            self.store.strip_bodies()  # a downgrade must not leave body text behind
        for attempt in (0, 1):
            try:
                return self._sync(account, mailbox, mode)
            except MailError as exc:
                if exc.code is ErrorCode.STALE_HANDLE and attempt == 0:
                    continue  # UIDVALIDITY changed mid-sync: start over once
                raise
        raise MailError(ErrorCode.INTERNAL, "mailbox kept changing during sync")  # pragma: no cover

    def sync_all(self, account: str, mailboxes: list[str] | None = None) -> list[SyncReport]:
        require_enabled(self.app)
        names = mailboxes or [i.name for i in self.app.store(account).list_mailboxes()
                              if i.selectable]
        reports = []
        for mb in names:
            try:
                reports.append(self.sync(account, mb))
            except MailError as exc:
                log.warning("index sync of %s failed: %s", mb, exc.code)
        return reports

    def _sync(self, account: str, mailbox: str, mode: StorageMode) -> SyncReport:
        report = SyncReport(account, mailbox)
        store = self.app.store(account)
        uidvalidity, uids = store.list_uids(mailbox)
        state = self.store.mailbox_state(account, mailbox)
        if state is not None and state["uidvalidity"] not in (None, uidvalidity):
            with self.app.db.tx() as c:
                self.store.wipe_mailbox(c, account, mailbox)
            report.reset = True
        indexed = self.store.indexed_flags(account, mailbox)

        current = set(uids)
        gone = sorted(set(indexed) - current)
        if gone:
            with self.app.db.tx() as c:
                report.removed = self.store.delete_uids(c, account, mailbox, gone)

        common = sorted(current & set(indexed))
        if common:
            report.flags_updated = self._refresh_flags(account, mailbox, uidvalidity,
                                                       common, indexed)

        new = sorted(current - set(indexed), reverse=True)  # newest first
        if len(new) > self.max_new:
            new, report.truncated = new[: self.max_new], True
        for i in range(0, len(new), CHUNK):
            chunk = new[i : i + CHUNK]
            summaries = store.fetch_summaries(mailbox, uidvalidity, chunk)
            bodies = {s.uid: self._body(account, mailbox, uidvalidity, s, report)
                      for s in summaries} if mode is StorageMode.INDEX else {}
            with self.app.db.tx() as c:
                for s in summaries:
                    row_id = self.store.upsert_message(c, account, uidvalidity, s)
                    body, att = bodies.get(s.uid, ("", ""))
                    self.store.write_fts(c, row_id, s, body, att)
                    report.added += 1
        with self.app.db.tx() as c:
            self.store.touch_mailbox(c, account, mailbox, uidvalidity)
        return report

    def _refresh_flags(self, account: str, mailbox: str, uidvalidity: int, uids: list[int],
                       indexed: dict[int, list[str]]) -> int:
        store = self.app.store(account)
        changes: dict[int, list[str]] = {}
        for i in range(0, len(uids), 500):
            fresh = store.fetch_flags(mailbox, uidvalidity, uids[i : i + 500])
            for uid, flags in fresh.items():
                if {f.lower() for f in flags} != {f.lower() for f in indexed.get(uid, [])}:
                    changes[uid] = list(flags)
        if changes:
            with self.app.db.tx() as c:
                self.store.update_flags(c, account, mailbox, changes)
        return len(changes)

    # ------------------------------------------------------------ bodies
    def _body(self, account: str, mailbox: str, uidvalidity: int, s: MessageSummary,
              report: SyncReport) -> tuple[str, str]:
        try:
            fetched = self.app.store(account).fetch_message(mailbox, uidvalidity, s.uid)
        except MailError as exc:
            if exc.code is ErrorCode.STALE_HANDLE:
                raise
            report.body_failures += 1
            return "", ""
        handle = MessageHandle(account=account, mailbox=mailbox, uidvalidity=uidvalidity,
                               uid=s.uid)
        msg = mime.parse_message(fetched, handle, include_headers=False, include_body=True,
                                 max_body_chars=self.app.config.max_body_chars,
                                 body_format="text")
        text = (msg.body.text if msg.body and msg.body.text else "") or ""
        att = ""
        if self.index_attachments:
            att = self._attachment_text(fetched.raw, msg.attachments)
        return text, att

    @staticmethod
    def _attachment_text(raw: bytes, attachments: list[Any]) -> str:
        parts: list[str] = []
        for a in attachments[:MAX_ATTACHMENT_PARTS]:
            if not supported(a.content_type):
                continue
            try:
                _, data = mime.get_part(raw, a.part_id)
            except MailError:
                continue
            text = extract_text(a.content_type, data)
            if text:
                parts.append(text)
        return "\n".join(parts)

    # ------------------------------------------------------------ journal driven
    def sync_from_events(self, account: str) -> list[SyncReport]:
        """Apply the change-event journal since the last call.

        Mailboxes named by message/flag/reset events are re-synced; a deleted
        mailbox is dropped. Requires the reconciler/watcher to be producing events.
        """
        require_enabled(self.app)
        events = EventStore(self.app)
        after = self.store.event_seq(account)
        touched: dict[str, None] = {}
        dropped: list[str] = []
        last = after
        while True:
            page = events.list(account, after_seq=last, limit=500)
            for ev in page.events:
                mb = ev["mailbox"]
                if mb is None:
                    continue
                if ev["type"] == "mailbox_deleted":
                    dropped.append(mb)
                    touched.pop(mb, None)
                elif ev["type"] in ("message_added", "message_removed", "flags_changed",
                                    "mailbox_reset", "label_membership_changed"):
                    touched[mb] = None
            last = page.next_cursor
            if not page.has_more:
                break
        if dropped:
            with self.app.db.tx() as c:
                for mb in dropped:
                    self.store.wipe_mailbox(c, account, mb)
        reports = []
        for mb in touched:
            try:
                reports.append(self.sync(account, mb))
            except MailError as exc:
                log.warning("index sync of %s failed: %s", mb, exc.code)
                return reports  # keep the cursor: retry these events next time
        if last != after:
            with self.app.db.tx() as c:
                self.store.set_event_seq(c, account, last)
        return reports
