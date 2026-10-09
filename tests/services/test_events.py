"""Change tracking: reconciliation, journal, watcher and policy."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from mcp_proton.domain.errors import ErrorCode, MailError
from mcp_proton.policy.model import ClientConfig, Preset
from mcp_proton.services.events import (
    EventStore,
    Reconciler,
    Watcher,
    list_events,
    purge_events,
    reconcile_now,
    watch_status,
)

from .conftest import ACCOUNT


def _types(app, **kw):
    return [e["type"] for e in EventStore(app).list(ACCOUNT, 0, 1000, **kw).events]


def _wait_for(predicate, timeout=8.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def rec(app):
    return Reconciler(app)


def test_first_run_is_baseline_without_events(app, rec, seed):
    seed("INBOX", 3)
    report = rec.reconcile(ACCOUNT, "INBOX")
    assert report.baseline and report.events == 0 and report.scanned == 3
    assert _types(app) == []
    again = rec.reconcile(ACCOUNT, "INBOX")
    assert not again.baseline and again.events == 0


def test_new_message_emits_message_added(app, rec, seed):
    rec.reconcile(ACCOUNT, "INBOX")
    [token] = seed("INBOX", 1, subject="Top secret subject")
    report = rec.reconcile(ACCOUNT, "INBOX")
    assert report.added == 1
    [ev] = EventStore(app).list(ACCOUNT).events
    assert ev["type"] == "message_added" and ev["mailbox"] == "INBOX"
    assert ev["data"]["handle"] == token and "\\Seen" not in ev["data"]["flags"]
    # nothing but handle/uid/flags: no subject, address or body text
    blob = json.dumps(ev)
    assert "Top secret" not in blob and "alice@example.com" not in blob and "Hi there" not in blob
    assert set(ev["data"]) == {"handle", "uid", "flags"}


def test_flag_change_emits_flags_changed(app, rec, seed):
    [token] = seed("INBOX", 1)
    rec.reconcile(ACCOUNT, "INBOX")
    from mcp_proton.domain.models import MessageHandle

    h = MessageHandle.parse(token)
    app.store(ACCOUNT).store_flags("INBOX", h.uidvalidity, [h.uid], add=["\\Seen"])
    report = rec.reconcile(ACCOUNT, "INBOX")
    assert report.flags_changed == 1
    [ev] = EventStore(app).list(ACCOUNT).events
    assert ev["type"] == "flags_changed"
    assert ev["data"]["added"] == ["\\Seen"] and ev["data"]["removed"] == []
    assert ev["data"]["handle"] == token


def test_removed_message_emits_message_removed(app, rec, seed):
    t1, t2 = seed("INBOX", 2)
    rec.reconcile(ACCOUNT, "INBOX")
    from mcp_proton.domain.models import MessageHandle

    h = MessageHandle.parse(t1)
    store = app.store(ACCOUNT)
    store.store_flags("INBOX", h.uidvalidity, [h.uid], add=["\\Deleted"])
    store.expunge_uids("INBOX", h.uidvalidity, [h.uid])
    report = rec.reconcile(ACCOUNT, "INBOX")
    assert report.removed == 1
    events = EventStore(app).list(ACCOUNT, types=["message_removed"]).events
    assert [e["data"]["handle"] for e in events] == [t1]


def test_uidvalidity_change_emits_reset_and_rebaselines(app, rec, seed):
    seed("INBOX", 2)
    rec.reconcile(ACCOUNT, "INBOX")
    with app.db.tx() as c:  # simulate the server having changed UIDVALIDITY
        c.execute("UPDATE mailbox_state SET uidvalidity = uidvalidity + 7 "
                  "WHERE account=? AND mailbox='INBOX'", (ACCOUNT,))
    report = rec.reconcile(ACCOUNT, "INBOX")
    assert report.reset and report.events == 1
    [ev] = EventStore(app).list(ACCOUNT).events
    assert ev["type"] == "mailbox_reset" and ev["data"]["messages"] == 2
    assert ev["data"]["previous_uidvalidity"] != ev["data"]["uidvalidity"]
    # rebaselined: no flood of message_added, and the next scan is quiet
    assert rec.reconcile(ACCOUNT, "INBOX").events == 0
    assert _types(app) == ["mailbox_reset"]


def test_recorded_state_and_scan_cost(app, rec, seed):
    seed("INBOX", 2)
    rec.reconcile(ACCOUNT, "INBOX")
    st = rec.load_state(ACCOUNT, "INBOX")
    assert st.last_reconciled_at and st.scan_ms is not None and len(st.flags) == 2


def test_label_mailbox_reports_membership_changes(make_app):
    app = make_app()
    app.config.watch.poll_labels = True
    store = app.store(ACCOUNT)
    store.create_mailbox("Labels/work")
    rec = Reconciler(app)
    assert "Labels/work" in rec.watch_scope(ACCOUNT)
    rec.reconcile_all(ACCOUNT)  # baseline
    store.append("Labels/work", b"Subject: x\r\nMessage-ID: <l1@x>\r\n\r\nbody\r\n")
    rec.reconcile_all(ACCOUNT)
    [ev] = EventStore(app).list(ACCOUNT, types=["label_membership_changed"]).events
    assert ev["mailbox"] == "Labels/work" and ev["data"]["change"] == "added"
    assert "message_added" not in _types(app)


def test_mailbox_created_and_deleted(app, rec):
    rec.reconcile_all(ACCOUNT)  # baseline of the mailbox list
    store = app.store(ACCOUNT)
    store.create_mailbox("Folders/projects")
    rec.reconcile_all(ACCOUNT)
    store.delete_mailbox("Folders/projects")
    rec.reconcile_all(ACCOUNT)
    events = EventStore(app).list(ACCOUNT).events
    assert [(e["type"], e["data"]["mailbox"]) for e in events] == [
        ("mailbox_created", "Folders/projects"), ("mailbox_deleted", "Folders/projects")]


def test_cursor_paging_and_type_filter(app, rec, seed, caller):
    rec.reconcile(ACCOUNT, "INBOX")
    seed("INBOX", 5)
    rec.reconcile(ACCOUNT, "INBOX")
    page1 = list_events(app, caller, ACCOUNT, limit=2)
    assert len(page1.events) == 2 and page1.has_more
    page2 = list_events(app, caller, ACCOUNT, cursor=page1.next_cursor, limit=2)
    page3 = list_events(app, caller, ACCOUNT, cursor=page2.next_cursor, limit=2)
    seqs = [e["seq"] for p in (page1, page2, page3) for e in p.events]
    assert seqs == sorted(set(seqs)) and len(seqs) == 5 and not page3.has_more
    # caught up: the cursor stays put
    empty = list_events(app, caller, ACCOUNT, cursor=page3.next_cursor)
    assert empty.events == [] and empty.next_cursor == page3.next_cursor
    assert list_events(app, caller, ACCOUNT, types=["flags_changed"]).events == []
    with pytest.raises(MailError) as exc:
        list_events(app, caller, ACCOUNT, types=["bogus"])
    assert exc.value.code is ErrorCode.INVALID_REQUEST
    with pytest.raises(MailError):
        list_events(app, caller, ACCOUNT, cursor="abc")


def test_purge_events(app):
    store = EventStore(app)
    store.append(ACCOUNT, "INBOX", "mailbox_reset", {"uidvalidity": 1})
    assert purge_events(app, datetime.now(UTC) - timedelta(days=1)) == 0
    assert purge_events(app, datetime.now(UTC) + timedelta(seconds=1)) == 1
    assert store.list(ACCOUNT).events == []


def test_reader_can_list_and_reconcile(make_app, caller, seed):
    app = make_app(Preset.READER)
    reconcile_now(app, caller, ACCOUNT, "INBOX")
    out = reconcile_now(app, caller, ACCOUNT)
    assert out["account"] == ACCOUNT and out["reports"]
    assert list_events(app, caller, ACCOUNT).events == []


def test_revoked_client_cannot_list(make_app, caller):
    app = make_app(clients=[ClientConfig(client_id="hermes", revoked=True)])
    for call in (lambda: list_events(app, caller, ACCOUNT),
                 lambda: reconcile_now(app, caller, ACCOUNT),
                 lambda: watch_status(app, caller, ACCOUNT)):
        with pytest.raises(MailError) as exc:
            call()
        assert exc.value.code is ErrorCode.CLIENT_REVOKED


def test_events_for_unreadable_mailboxes_are_filtered(make_app, caller, seed):
    from mcp_proton.domain.families import OperationFamily
    from mcp_proton.policy.model import Action, PolicyRule

    app = make_app(rules=[PolicyRule(family=OperationFamily.READ, action=Action.DENY,
                                     mailbox="Archive")])
    rec = Reconciler(app)
    rec.reconcile(ACCOUNT, "INBOX")
    rec.reconcile(ACCOUNT, "Archive")
    app.store(ACCOUNT).append("INBOX", b"Subject: a\r\n\r\nb\r\n")
    app.store(ACCOUNT).append("Archive", b"Subject: a\r\n\r\nb\r\n")
    rec.reconcile(ACCOUNT, "INBOX")
    rec.reconcile(ACCOUNT, "Archive")
    page = list_events(app, caller, ACCOUNT)
    assert [e["mailbox"] for e in page.events] == ["INBOX"]
    assert page.next_cursor == 2  # the hidden event still advances the cursor


def test_watch_status_freshness(app, caller):
    status = watch_status(app, caller, ACCOUNT)
    assert status["scope"] == ["INBOX"] and status["idle_active"] is False
    assert status["mailboxes"][0]["freshness_seconds"] is None
    reconcile_now(app, caller, ACCOUNT, "INBOX")
    status = watch_status(app, caller, ACCOUNT)
    mb = status["mailboxes"][0]
    assert 0 <= mb["freshness_seconds"] < 5 and mb["scan_ms"] is not None
    assert status["poll_interval_seconds"] == app.config.watch.poll_interval_seconds


def test_watcher_idle_picks_up_new_mail_and_stops_promptly(app, caller):
    watcher = Watcher(app, poll_interval=30, idle_slice=1.0)
    watcher.start()
    try:
        assert _wait_for(lambda: Reconciler(app).load_state(ACCOUNT, "INBOX") is not None)
        assert _wait_for(lambda: watch_status(app, caller, ACCOUNT)["idle_active"])
        app.store(ACCOUNT).append("INBOX", b"Subject: hi\r\nMessage-ID: <w1@x>\r\n\r\nbody\r\n")
        assert _wait_for(lambda: "message_added" in _types(app), timeout=8)
        assert watch_status(app, caller, ACCOUNT)["watcher_running"] is True
    finally:
        t0 = time.monotonic()
        watcher.stop(timeout=5)
        assert time.monotonic() - t0 < 4
    assert not watcher.running
    assert watch_status(app, caller, ACCOUNT)["idle_active"] is False


def test_watcher_polling_only_when_idle_disabled(app, caller):
    app.config.watch.idle = False
    watcher = Watcher(app, poll_interval=0.3)
    watcher.start()
    try:
        assert _wait_for(lambda: Reconciler(app).load_state(ACCOUNT, "INBOX") is not None)
        assert watch_status(app, caller, ACCOUNT)["idle_mailbox"] is None
        app.store(ACCOUNT).append("INBOX", b"Subject: hi\r\nMessage-ID: <w2@x>\r\n\r\nbody\r\n")
        assert _wait_for(lambda: "message_added" in _types(app), timeout=8)
        assert watch_status(app, caller, ACCOUNT)["idle_active"] is False
    finally:
        watcher.stop()


def test_watcher_survives_bridge_errors(app):
    app.config.watch.idle = False
    store = app.store(ACCOUNT)
    real = store.list_uids
    calls = {"n": 0}

    def flaky(mailbox, criteria=None):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise MailError(ErrorCode.BRIDGE_UNAVAILABLE, "down")
        return real(mailbox, criteria)

    store.list_uids = flaky  # type: ignore[method-assign]
    watcher = Watcher(app, poll_interval=0.2, backoff_initial=0.05, backoff_cap=0.2)
    watcher.start()
    try:
        assert _wait_for(lambda: Reconciler(app).load_state(ACCOUNT, "INBOX") is not None)
    finally:
        watcher.stop()
