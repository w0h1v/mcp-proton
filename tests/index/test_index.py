from __future__ import annotations

import json

import pytest
from tests.index.conftest import ACCOUNT

from mcp_proton.config import StorageMode
from mcp_proton.domain.errors import ErrorCode, MailError
from mcp_proton.domain.families import OperationFamily
from mcp_proton.domain.models import SearchQuery
from mcp_proton.index import (
    Indexer,
    IndexStore,
    delete_saved_search,
    export_index,
    extract_text,
    list_saved_searches,
    local_search,
    message_stats,
    purge,
    run_saved_search,
    save_search,
    storage_report,
)
from mcp_proton.policy.model import Action, Constraints, PolicyRule


def _count(app, table="idx_messages"):
    return app.db.query(f"SELECT COUNT(*) FROM {table}")[0][0]


# ------------------------------------------------------------------ sync


def test_metadata_sync_caches_headers_only(app, seed, caller):
    app.config.storage_mode = StorageMode.METADATA
    seed("INBOX", subject="Quarterly budget", body="zebra secret body")
    rep = Indexer(app).sync(ACCOUNT, "INBOX")
    assert rep.added == 1 and not rep.reset
    row = app.db.query("SELECT * FROM idx_messages")[0]
    assert row["subject"] == "Quarterly budget" and row["body_indexed"] == 0
    assert json.loads(row["from_json"])[0]["email"] == "alice@example.com"
    # subject is searchable, body is not
    assert local_search(app, caller, ACCOUNT, "budget").total == 1
    assert local_search(app, caller, ACCOUNT, "zebra").total == 0


def test_fts_search_finds_body_words(index_app, indexer, seed, caller):
    seed("INBOX", subject="Hello", body="the pangolin migrated north")
    seed("INBOX", subject="Other", body="nothing relevant")
    indexer.sync(ACCOUNT, "INBOX")
    res = local_search(index_app, caller, ACCOUNT, "pangolin")
    assert res.source == "local_index" and res.total == 1
    assert res.items[0].subject == "Hello"
    assert "[pangolin]" in res.items[0].snippet
    assert res.freshness is not None and res.freshness_by_mailbox["INBOX"] == res.freshness
    assert res.items[0].handle.startswith("h1.")


def test_search_text_is_not_fts_syntax(index_app, indexer, seed, caller):
    seed("INBOX", body="plain words")
    indexer.sync(ACCOUNT, "INBOX")
    for q in ['"unbalanced', "a OR NEAR(", "col:evil", "***"]:
        try:
            local_search(index_app, caller, ACCOUNT, q)
        except MailError as e:
            assert e.code is ErrorCode.INVALID_REQUEST
    assert local_search(index_app, caller, ACCOUNT, "plai*").total == 1


def test_removed_messages_disappear_after_sync(index_app, indexer, seed, caller):
    h = seed("INBOX", n=2, body="walrus")
    indexer.sync(ACCOUNT, "INBOX")
    assert local_search(index_app, caller, ACCOUNT, "walrus").total == 2
    from mcp_proton.domain.models import MessageHandle

    mh = MessageHandle.parse(h[0])
    st = index_app.store(ACCOUNT)
    st.store_flags("INBOX", mh.uidvalidity, [mh.uid], add=["\\Deleted"])
    st.expunge_uids("INBOX", mh.uidvalidity, [mh.uid])
    rep = indexer.sync(ACCOUNT, "INBOX")
    assert rep.removed == 1
    assert local_search(index_app, caller, ACCOUNT, "walrus").total == 1
    assert _count(index_app) == 1
    assert index_app.db.query("SELECT COUNT(*) FROM idx_fts")[0][0] == 1


def test_flag_changes_are_refreshed(index_app, indexer, seed):
    h = seed("INBOX")
    indexer.sync(ACCOUNT, "INBOX")
    from mcp_proton.domain.models import MessageHandle

    mh = MessageHandle.parse(h[0])
    index_app.store(ACCOUNT).store_flags("INBOX", mh.uidvalidity, [mh.uid], add=["\\Seen"])
    assert indexer.sync(ACCOUNT, "INBOX").flags_updated == 1
    assert index_app.db.query("SELECT seen FROM idx_messages")[0][0] == 1


def test_uidvalidity_change_wipes_mailbox(index_app, indexer, seed):
    seed("INBOX", n=2)
    indexer.sync(ACCOUNT, "INBOX")
    with index_app.db.tx() as c:
        c.execute("UPDATE idx_mailboxes SET uidvalidity = uidvalidity + 1000")
        c.execute("UPDATE idx_messages SET uidvalidity = uidvalidity + 1000")
    rep = indexer.sync(ACCOUNT, "INBOX")
    assert rep.reset and rep.added == 2
    uvs = {r[0] for r in index_app.db.query("SELECT DISTINCT uidvalidity FROM idx_messages")}
    real_uv = index_app.store(ACCOUNT).list_uids("INBOX")[0]
    assert uvs == {real_uv} and _count(index_app) == 2


def test_live_mode_is_a_noop(app, seed, caller, owner):
    seed("INBOX", body="anything")
    assert app.config.storage_mode is StorageMode.LIVE
    ix = Indexer(app)
    for fn in (lambda: ix.sync(ACCOUNT, "INBOX"), lambda: ix.sync_all(ACCOUNT),
               lambda: ix.sync_from_events(ACCOUNT),
               lambda: local_search(app, caller, ACCOUNT, "anything"),
               lambda: message_stats(app, caller, ACCOUNT)):
        with pytest.raises(MailError) as e:
            fn()
        assert e.value.code is ErrorCode.UNSUPPORTED
    assert _count(app) == 0 and _count(app, "idx_mailboxes") == 0
    assert storage_report(app)["rows"]["idx_messages"] == 0


def test_sync_from_events(index_app, indexer, seed, caller):
    from mcp_proton.services.events import Reconciler

    seed("INBOX", subject="old")
    rec = Reconciler(index_app)
    rec.reconcile(ACCOUNT, "INBOX")  # baseline
    indexer.sync_from_events(ACCOUNT)  # nothing yet
    seed("INBOX", subject="fresh arrival", body="giraffe")
    rec.reconcile(ACCOUNT, "INBOX")
    reports = indexer.sync_from_events(ACCOUNT)
    assert [r.mailbox for r in reports] == ["INBOX"]
    assert local_search(index_app, caller, ACCOUNT, "giraffe").total == 1
    assert indexer.sync_from_events(ACCOUNT) == []  # cursor advanced


def test_downgrade_to_metadata_strips_bodies(index_app, indexer, seed, caller):
    seed("INBOX", body="okapi")
    indexer.sync(ACCOUNT, "INBOX")
    assert local_search(index_app, caller, ACCOUNT, "okapi").total == 1
    index_app.config.storage_mode = StorageMode.METADATA
    Indexer(index_app).sync(ACCOUNT, "INBOX")
    assert local_search(index_app, caller, ACCOUNT, "okapi").total == 0


def test_attachment_text_indexing(index_app, seed, caller):
    raw = (b"From: a@example.com\r\nTo: demouser@x.test\r\nSubject: Report\r\n"
           b"Message-ID: <m1@example.com>\r\nMIME-Version: 1.0\r\n"
           b"Content-Type: multipart/mixed; boundary=XX\r\n\r\n--XX\r\n"
           b"Content-Type: text/plain\r\n\r\nsee attached\r\n--XX\r\n"
           b"Content-Type: text/csv; name=\"d.csv\"\r\n"
           b"Content-Disposition: attachment; filename=\"d.csv\"\r\n\r\n"
           b"id,name\r\n1,axolotl\r\n--XX--\r\n")
    index_app.store(ACCOUNT).append("INBOX", raw)
    Indexer(index_app).sync(ACCOUNT, "INBOX")
    assert local_search(index_app, caller, ACCOUNT, "axolotl").total == 0
    Indexer(index_app, index_attachments=True)
    with index_app.db.tx() as c:
        c.execute("DELETE FROM idx_messages")
        c.execute("DELETE FROM idx_mailboxes")
    Indexer(index_app, index_attachments=True).sync(ACCOUNT, "INBOX")
    assert local_search(index_app, caller, ACCOUNT, "axolotl").total == 1


def test_extract_text_supported_types_only():
    assert extract_text("text/csv", b"a,b\n1,2") == "a,b\n1,2"
    assert "hello" in extract_text("text/html", b"<p>hello</p>")
    assert "val" in extract_text("application/json", b'{"k": ["val"]}')
    assert extract_text("application/pdf", b"%PDF-1.4") is None
    assert extract_text("text/plain", b"x" * (3 * 1024 * 1024)) is None


# ------------------------------------------------------------------ policy


def test_deny_rule_hides_mailbox_results(make_app, seed_for, caller):
    app = make_app(rules=[PolicyRule(family=OperationFamily.READ, action=Action.DENY,
                                     mailbox="Archive")])
    app.config.storage_mode = StorageMode.INDEX
    seed_for(app, "INBOX", body="narwhal inbox")
    seed_for(app, "Archive", body="narwhal archive")
    ix = Indexer(app)
    ix.sync(ACCOUNT, "INBOX")
    ix.sync(ACCOUNT, "Archive")
    res = local_search(app, caller, ACCOUNT, "narwhal")
    assert res.scope == ["INBOX"] and {h.mailbox for h in res.items} == {"INBOX"}
    assert any("excluded by policy" in n for n in res.notes)
    res = local_search(app, caller, ACCOUNT, "narwhal", mailboxes=["Archive"])
    assert res.items == [] and res.scope == []
    stats = message_stats(app, caller, ACCOUNT)
    assert set(stats["mailboxes"]) == {"INBOX"}


def test_paused_account_blocks_local_search(make_app, seed_for, caller):
    app = make_app(paused_accounts=[ACCOUNT])
    app.config.storage_mode = StorageMode.INDEX
    with pytest.raises(MailError) as e:
        local_search(app, caller, ACCOUNT, "x")
    assert e.value.code is ErrorCode.ACCOUNT_PAUSED


def test_unreleased_bodies_are_not_searchable(make_app, seed_for, caller):
    app = make_app(constraints=Constraints(release_bodies=False))
    app.config.storage_mode = StorageMode.INDEX
    seed_for(app, "INBOX", subject="Visible subject", body="confidential llama")
    Indexer(app).sync(ACCOUNT, "INBOX")
    assert local_search(app, caller, ACCOUNT, "llama").total == 0
    res = local_search(app, caller, ACCOUNT, "visible")
    assert res.total == 1 and res.items[0].snippet is None
    assert res.searched_fields == ["subject", "addrs"]
    assert message_stats(app, caller, ACCOUNT)["top_senders"] is None


# ------------------------------------------------------------------ stats / saved


def test_message_stats(index_app, indexer, seed, caller):
    seed("INBOX", n=2, frm="bob@example.com")
    seed("INBOX", n=1, frm="carol@example.com", flags=["\\Seen"])
    indexer.sync(ACCOUNT, "INBOX")
    s = message_stats(index_app, caller, ACCOUNT)
    assert s["mailboxes"]["INBOX"] == {"total": 3, "unread": 2}
    assert s["top_senders"][0] == {"address": "bob@example.com", "count": 2}
    assert s["source"] == "local_index" and s["freshness"]


def test_saved_searches_crud(index_app, indexer, seed, caller, owner):
    seed("INBOX", subject="Invoice 42", body="please pay")
    indexer.sync(ACCOUNT, "INBOX")
    s = save_search(index_app, caller, ACCOUNT, "invoices", "invoice")
    assert s.kind == "text" and s.query == "invoice"
    save_search(index_app, caller, ACCOUNT, "from-alice",
                SearchQuery(**{"from": "alice@example.com"}), ["INBOX"])
    assert [x.name for x in list_saved_searches(index_app, caller, ACCOUNT)] == [
        "from-alice", "invoices"]
    # replace
    save_search(index_app, caller, ACCOUNT, "invoices", "pay")
    assert run_saved_search(index_app, caller, ACCOUNT, "invoices").total == 1
    live = run_saved_search(index_app, caller, ACCOUNT, "from-alice")
    assert live.total == 1  # structured search goes through the live IMAP path
    # isolation between clients, owner sees all
    from mcp_proton.domain.requests import CallerContext

    other = CallerContext(client_id="other")
    assert list_saved_searches(index_app, other, ACCOUNT) == []
    with pytest.raises(MailError):
        run_saved_search(index_app, other, ACCOUNT, "invoices")
    assert len(list_saved_searches(index_app, owner, ACCOUNT)) == 2
    assert delete_saved_search(index_app, caller, ACCOUNT, "invoices") is True
    assert delete_saved_search(index_app, caller, ACCOUNT, "invoices") is False
    with pytest.raises(MailError):
        save_search(index_app, caller, ACCOUNT, "bad/name", "x")


# ------------------------------------------------------------------ purge / export / report


def test_purge_export_and_report(index_app, indexer, seed, caller, owner, tmp_path):
    seed("INBOX", n=2, body="kakapo")
    indexer.sync(ACCOUNT, "INBOX")
    save_search(index_app, caller, ACCOUNT, "k", "kakapo")

    out = tmp_path / "export" / "idx.jsonl"
    assert export_index(index_app, owner, out) == 2
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert rows[0]["mailbox"] == "INBOX" and "kakapo" not in out.read_text()
    assert oct(out.stat().st_mode & 0o777) == "0o600"

    rep = storage_report(index_app)
    assert rep["encrypted"] is False and rep["rows"]["idx_messages"] == 2
    assert rep["accounts"][ACCOUNT]["body_indexed"] == 2
    assert rep["database_bytes"] > 0 and rep["accounts"][ACCOUNT]["oldest_record"]
    assert rep["operational_rows"]["operations"] == 0
    limited = storage_report(index_app, caller)
    assert "database_path" not in limited and limited["accounts"][ACCOUNT]["messages"] == 2

    with pytest.raises(MailError):
        purge(index_app, caller)
    with pytest.raises(MailError):
        export_index(index_app, caller, out)
    before = index_app.db.query("PRAGMA secure_delete")[0][0]
    assert purge(index_app, owner, ACCOUNT)["messages"] == 2
    assert _count(index_app) == 0 and _count(index_app, "idx_mailboxes") == 0
    assert index_app.db.query("SELECT COUNT(*) FROM idx_fts")[0][0] == 0
    assert _count(index_app, "saved_searches") == 1  # kept unless asked
    assert index_app.db.query("PRAGMA secure_delete")[0][0] == before  # restored
    assert purge(index_app, owner, include_saved_searches=True)["saved_searches"] == 1
    # a purge forces a clean re-sync
    indexer.sync(ACCOUNT, "INBOX")
    assert _count(index_app) == 2


def test_purge_older_than_keeps_recent(index_app, indexer, seed, owner):
    from datetime import UTC, datetime, timedelta

    seed("INBOX", n=2)
    indexer.sync(ACCOUNT, "INBOX")
    with index_app.db.tx() as c:
        c.execute("UPDATE idx_messages SET indexed_at='2000-01-01T00:00:00+00:00' "
                  "WHERE uid=(SELECT MIN(uid) FROM idx_messages)")
    assert purge(index_app, owner, older_than=datetime.now(UTC) - timedelta(days=1))[
        "messages"] == 1
    assert _count(index_app) == 1


def test_report_warns_when_live_but_data_remains(index_app, indexer, seed):
    seed("INBOX")
    indexer.sync(ACCOUNT, "INBOX")
    index_app.config.storage_mode = StorageMode.LIVE
    assert "warning" in storage_report(index_app)
    assert IndexStore(index_app).count("idx_messages") == 1
