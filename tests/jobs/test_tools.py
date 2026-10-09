"""The MCP tool family through FastMCP's in-memory client (stdio-equivalent)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastmcp import Client
from tests.jobs.conftest import ACCOUNT

from mcp_proton.mcp.server import build_server

TOOLS = ["jobs_schedule_send", "jobs_list", "jobs_get", "jobs_edit", "jobs_cancel",
         "jobs_snooze", "jobs_remind", "rules_create", "rules_preview", "rules_run",
         "webhooks_create", "webhooks_list", "webhooks_delete", "webhooks_reveal_secret",
         "operations_undo"]


@pytest.fixture(autouse=True)
def _client_label(monkeypatch):
    monkeypatch.setenv("MCP_PROTON_CLIENT_ID", "hermes")


async def call(c: Client, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    res = await c.call_tool(name, args or {}, raise_on_error=False)
    if res.is_error:
        return {"error": json.loads(res.content[0].text)}  # type: ignore[union-attr]
    return res.structured_content or {}


def client_for(app) -> Client:
    app.config.watch.mailboxes = []
    return Client(build_server(app, transport="stdio"))


def in_hours(h: float) -> str:
    return (datetime.now(UTC) + timedelta(hours=h)).isoformat()


async def test_tools_listed_with_annotations(app):
    async with client_for(app) as c:
        tools = {t.name: t for t in await c.list_tools()}
    for name in TOOLS:
        assert name in tools, name
    assert tools["jobs_list"].annotations.read_only_hint is True
    assert tools["rules_preview"].annotations.read_only_hint is True
    assert tools["jobs_schedule_send"].annotations.read_only_hint is False
    assert "not Proton's native scheduled send" in tools["jobs_schedule_send"].description


async def test_schedule_list_get_cancel(app):
    async with client_for(app) as c:
        out = await call(c, "jobs_schedule_send", {
            "account": ACCOUNT, "to": ["bob@example.com"], "subject": "Later", "text": "zebra-body",
            "at": in_hours(2)})
        assert out["status"] == "succeeded", out
        job_id = out["result"]["job_id"]
        listing = await call(c, "jobs_list", {})
        assert [j["job_id"] for j in listing["items"]] == [job_id]
        got = await call(c, "jobs_get", {"job_id": job_id})
        assert got["status"] == "active" and got["runs"] == []
        assert "zebra-body" not in json.dumps(got)  # no body in the job view
        cancelled = await call(c, "jobs_cancel", {"job_id": job_id})
        assert cancelled["status"] == "succeeded"
        missing = await call(c, "jobs_get", {"job_id": "job_nope"})
        assert missing["error"]["code"] == "not_found"


async def test_assistant_gets_approval_pending(make_app):
    from mcp_proton.policy.model import Preset

    app = make_app(Preset.ASSISTANT)
    async with client_for(app) as c:
        out = await call(c, "jobs_schedule_send", {
            "account": ACCOUNT, "to": ["bob@example.com"], "subject": "Later", "text": "hi",
            "at": in_hours(2)})
    assert out["status"] == "approval_pending" and out["operation_id"]


async def test_webhook_secret_returned_once(app):
    async with client_for(app) as c:
        out = await call(c, "webhooks_create", {"account": ACCOUNT,
                                                "url": "http://127.0.0.1:9/hook"})
        assert out["result"]["secret"].startswith("whsec_")
        again = await call(c, "webhooks_reveal_secret",
                           {"webhook_id": out["result"]["webhook_id"]})
        assert again["error"]["code"] == "conflict"
        listing = await call(c, "webhooks_list")
        assert "whsec_" not in json.dumps(listing)
        bad = await call(c, "webhooks_create", {"account": ACCOUNT, "url": "http://example.com/x"})
        assert bad["error"]["code"] == "invalid_request"


async def test_undo_tool(app, seed):
    from mcp_proton.domain.requests import CallerContext
    from mcp_proton.services import messages

    [h] = seed()
    out = messages.archive(app, CallerContext(client_id="hermes"), [h])
    async with client_for(app) as c:
        res = await call(c, "operations_undo", {"operation_id": out.operation_id})
        assert res["status"] == "succeeded"
        again = await call(c, "operations_undo", {"operation_id": out.operation_id})
        assert again["error"]["code"] == "conflict"
