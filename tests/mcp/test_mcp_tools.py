"""Tools through FastMCP's in-memory client (the stdio-equivalent path)."""

from __future__ import annotations

import pytest

from conftest import ACCOUNT, call, client_for  # type: ignore[import-not-found]
from mcp_proton.domain.models import OperationStatus
from mcp_proton.policy.model import Preset

FAMILIES = ["accounts_", "mailboxes_", "messages_", "attachments_", "labels_", "drafts_",
            "mail_", "imports_", "exports_", "events_", "conversations_", "operations_"]


async def test_tool_families_and_annotations(app):
    async with client_for(app) as c:
        tools = {t.name: t for t in await c.list_tools()}
    for prefix in FAMILIES:
        assert any(n.startswith(prefix) for n in tools), prefix
    for name in ("mail_send", "mail_reply", "mail_forward", "mail_reconcile",
                 "operations_status", "operations_resume", "operations_cancel"):
        assert name in tools
    # owner administration is not exposed over MCP
    assert not [n for n in tools if "policy" in n or "approve" in n or "owner" in n]

    for name in ("accounts_list", "mailboxes_list", "messages_list", "messages_get",
                 "messages_search", "operations_status", "mail_reconcile"):
        a = tools[name].annotations
        assert a.read_only_hint is True and a.destructive_hint is False, name
    for name in ("messages_expunge", "mailboxes_delete_folder", "labels_delete",
                 "mailboxes_empty", "drafts_discard", "mail_send", "drafts_send"):
        assert tools[name].annotations.destructive_hint is True, name
    for name in ("mail_send", "mail_reply", "mail_forward", "drafts_send"):
        assert tools[name].annotations.open_world_hint is True, name
    assert tools["messages_set_flags"].annotations.idempotent_hint is True
    assert tools["messages_move"].annotations.read_only_hint is False
    assert "untrusted" in tools["messages_get"].description
    assert "approval_pending" in tools["messages_move"].description
    assert "operations_resume" in tools["mail_send"].description


async def test_read_tools(app, seed):
    [h] = seed(subject="Quarterly numbers", body="see attached")
    async with client_for(app) as c:
        accts = await call(c, "accounts_list")
        assert accts["items"][0]["name"] == ACCOUNT
        boxes = await call(c, "mailboxes_list", {"account": ACCOUNT, "with_counts": True})
        assert any(m["name"] == "INBOX" for m in boxes["items"])
        listing = await call(c, "messages_list", {"account": ACCOUNT, "mailbox": "INBOX"})
        assert [m["handle"] for m in listing["items"]] == [h]
        got = await call(c, "messages_get", {"handle": h})
        assert got["message"]["subject"] == "Quarterly numbers"
        assert got["message"]["from"][0]["email"] == "alice@example.com"  # alias kept
        assert "untrusted" in got["notice"]
        found = await call(c, "messages_search", {
            "account": ACCOUNT, "query": {"subject": "Quarterly"}})
        assert [m["handle"] for m in found["items"]] == [h]
        att = await call(c, "attachments_list", {"handle": h})
        assert att["items"] == []
        status = await call(c, "events_watch_status", {"account": ACCOUNT})
        assert status["account"] == ACCOUNT


async def test_pagination_is_bounded(app, seed):
    seed(n=3)
    app.config.max_page_size = 2
    async with client_for(app) as c:
        too_big = await call(c, "messages_list", {"account": ACCOUNT, "mailbox": "INBOX",
                                                  "limit": 500})
        assert "error" in too_big
        first = await call(c, "messages_list", {"account": ACCOUNT, "mailbox": "INBOX",
                                                "limit": 2})
        assert len(first["items"]) == 2 and first["next_cursor"]
        second = await call(c, "messages_list", {"account": ACCOUNT, "mailbox": "INBOX",
                                                  "limit": 2, "cursor": first["next_cursor"]})
        assert len(second["items"]) == 1


async def test_autonomous_write_succeeds(app, seed):
    [h] = seed()
    async with client_for(app) as c:
        out = await call(c, "messages_trash", {"handles": [h]})
        assert out["status"] == "succeeded" and out["operation_id"]
        assert out["items"][0]["new_handle"]
        trash = await call(c, "messages_list", {"account": ACCOUNT, "mailbox": "Trash"})
        assert len(trash["items"]) == 1
        made = await call(c, "mailboxes_create_folder", {"account": ACCOUNT, "name": "Projects"})
        assert made["status"] == "succeeded"


async def test_autonomous_send(app, smtp_server):
    async with client_for(app) as c:
        out = await call(c, "mail_send", {
            "account": ACCOUNT, "to": ["bob@example.com", "Carol <carol@example.com>"],
            "subject": "Hi", "text": "hello"})
    assert out["status"] == "succeeded", out
    assert len(smtp_server.messages) == 1
    assert sorted(smtp_server.messages[0][1]) == ["bob@example.com", "carol@example.com"]


async def test_assistant_send_pending_then_resume_once(make_app, owner, smtp_server):
    app = make_app(Preset.ASSISTANT)
    async with client_for(app) as c:
        out = await call(c, "mail_send", {"account": ACCOUNT, "to": ["bob@example.com"],
                                          "subject": "Plan", "text": "details"})
        assert out["status"] == "approval_pending"
        assert out["operation_id"] and out["expires_at"] and "Send" in out["summary"]
        assert smtp_server.messages == []

        # not approved yet: resume reports pending and sends nothing
        again = await call(c, "operations_resume", {"operation_id": out["operation_id"]})
        assert again["status"] == "approval_pending"
        status = await call(c, "operations_status", {"operation_id": out["operation_id"]})
        assert status["status"] == OperationStatus.PENDING.value

        app.approve(owner, out["operation_id"])
        done = await call(c, "operations_resume", {"operation_id": out["operation_id"]})
        assert done["status"] == "succeeded"
        replay = await call(c, "operations_resume", {"operation_id": out["operation_id"]})
        assert replay["status"] == "succeeded"
    assert len(smtp_server.messages) == 1


async def test_owner_denial_blocks_resume(make_app, owner, smtp_server):
    app = make_app(Preset.ASSISTANT)
    async with client_for(app) as c:
        out = await call(c, "mail_send", {"account": ACCOUNT, "to": ["bob@example.com"],
                                          "subject": "x", "text": "y"})
        app.approve(owner, out["operation_id"], approve=False)
        res = await call(c, "operations_resume", {"operation_id": out["operation_id"]})
        assert res["status"] == "denied"
    assert smtp_server.messages == []


async def test_operations_are_caller_scoped(make_app, monkeypatch):
    app = make_app(Preset.ASSISTANT)
    async with client_for(app) as c:
        out = await call(c, "mail_send", {"account": ACCOUNT, "to": ["bob@example.com"],
                                          "subject": "x", "text": "y"})
    op = out["operation_id"]
    monkeypatch.setenv("MCP_PROTON_CLIENT_ID", "other-agent")
    async with client_for(app) as c:
        for tool in ("operations_status", "operations_resume", "operations_cancel"):
            res = await call(c, tool, {"operation_id": op})
            assert res["error"]["code"] == "not_found", tool
    monkeypatch.setenv("MCP_PROTON_CLIENT_ID", "hermes")
    async with client_for(app) as c:
        res = await call(c, "operations_status", {"operation_id": op})
        assert res["status"] == "pending"


async def test_cancel_pending_operation(make_app, smtp_server, owner):
    app = make_app(Preset.ASSISTANT)
    async with client_for(app) as c:
        out = await call(c, "mail_send", {"account": ACCOUNT, "to": ["bob@example.com"],
                                          "subject": "x", "text": "y"})
        res = await call(c, "operations_cancel", {"operation_id": out["operation_id"]})
        assert res["status"] == "cancelled"
    assert smtp_server.messages == []


async def test_reader_write_is_policy_denied(make_app, seed):
    app = make_app(Preset.READER)
    [h] = seed()
    async with client_for(app) as c:
        res = await call(c, "messages_trash", {"handles": [h]})
        err = res["error"]
        assert err["code"] == "policy_denied"
        assert err["message"] and isinstance(err["details"], dict)
        send = await call(c, "mail_send", {"account": ACCOUNT, "to": ["bob@example.com"],
                                           "subject": "x", "text": "y"})
        assert send["error"]["code"] == "policy_denied"
        # reads still work
        assert (await call(c, "messages_get", {"handle": h}))["message"]["subject"]


async def test_not_onboarded_is_denied(make_app):
    app = make_app(None)
    async with client_for(app) as c:
        res = await call(c, "accounts_list")
        assert res["error"]["code"] == "not_onboarded"


async def test_revoked_client_denied(make_app):
    from mcp_proton.policy.model import ClientConfig

    app = make_app(Preset.AUTONOMOUS, clients=[ClientConfig(client_id="hermes", revoked=True)])
    async with client_for(app) as c:
        res = await call(c, "accounts_list")
        assert res["error"]["code"] == "client_revoked"


async def test_validation_errors_do_not_echo_input(app):
    async with client_for(app) as c:
        res = await call(c, "mail_send", {"account": ACCOUNT, "to": ["not-an-address"],
                                          "subject": "SECRET-SUBJECT", "text": "SECRET-BODY"})
    assert res["error"]["code"] == "invalid_request"
    assert "SECRET" not in str(res)


async def test_unexpected_errors_are_masked(app, monkeypatch):
    from mcp_proton.services import accounts

    def boom(*a, **k):
        raise RuntimeError("secret-internal-detail")

    monkeypatch.setattr(accounts, "list_accounts", boom)
    async with client_for(app) as c:
        res = await c.call_tool("accounts_list", {}, raise_on_error=False)
    assert res.is_error
    assert "secret-internal-detail" not in str(res.content)


async def test_drafts_lifecycle(app, smtp_server):
    async with client_for(app) as c:
        made = await call(c, "drafts_create", {
            "account": ACCOUNT, "to": ["bob@example.com"], "subject": "Draft", "text": "v1"})
        assert made["status"] == "succeeded", made
        handle = made["result"]["handle"]
        got = await call(c, "drafts_get", {"handle": handle})
        assert got["draft"]["subject"] == "Draft"
        upd = await call(c, "drafts_update", {"handle": handle,
                                              "changes": {"text": "v2", "cc": ["x@example.com"]}})
        assert upd["status"] == "succeeded", upd
        new = upd["result"]["handle"]
        bad = await call(c, "drafts_update", {"handle": new, "changes": {}})
        assert bad["error"]["code"] == "invalid_request"
        sent = await call(c, "drafts_send", {"handle": new})
        assert sent["status"] == "succeeded", sent
    assert len(smtp_server.messages) == 1


async def test_reply_and_reconcile(app, seed, smtp_server):
    [h] = seed(subject="Question", frm="alice@example.com")
    async with client_for(app) as c:
        out = await call(c, "mail_reply", {"handle": h, "text": "Answer"})
        assert out["status"] == "succeeded", out
        rec = await call(c, "mail_reconcile", {"operation_id": out["operation_id"]})
        assert rec["resent"] is False
    assert smtp_server.messages[0][1] == ["alice@example.com"]


async def test_events_and_conversation(app, seed):
    [h] = seed(subject="Thread")
    async with client_for(app) as c:
        conv = await call(c, "conversations_get", {"handle": h})
        assert conv["nodes"]
        rec = await call(c, "events_reconcile", {"account": ACCOUNT, "mailbox": "INBOX"})
        assert rec["account"] == ACCOUNT
        ev = await call(c, "events_list", {"account": ACCOUNT})
        assert "events" in ev and "next_cursor" in ev


@pytest.mark.parametrize("prompt", ["triage_inbox", "draft_reply", "inbox_review"])
async def test_prompts_warn_about_untrusted_content(app, prompt):
    args = {"triage_inbox": {"account": ACCOUNT},
            "draft_reply": {"handle": "h1.x", "intent": "agree"},
            "inbox_review": {"account": ACCOUNT}}[prompt]
    async with client_for(app) as c:
        names = {p.name for p in await c.list_prompts()}
        assert {"triage_inbox", "draft_reply", "inbox_review"} <= names
        res = await c.get_prompt(prompt, args)
    text = res.messages[0].content.text
    assert "untrusted" in text and "prompt injection" in text
