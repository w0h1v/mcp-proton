"""Server assembly: identity labels, lifespan, optional modules, tool search."""

from __future__ import annotations

import fastmcp
import pytest
from fastmcp import Client

from conftest import ACCOUNT, call, client_for  # type: ignore[import-not-found]
from mcp_proton.domain.errors import MailError
from mcp_proton.domain.requests import Transport
from mcp_proton.mcp import server as server_mod
from mcp_proton.mcp.identity import Identity
from mcp_proton.mcp.server import build_server
from mcp_proton.policy.model import Preset


def test_stdio_identity_is_an_unauthenticated_label(app, monkeypatch):
    ident = Identity(app, "stdio")
    caller = ident.caller()
    assert (caller.client_id, caller.transport, caller.authenticated) == (
        "hermes", Transport.STDIO, False)
    assert not caller.is_owner
    monkeypatch.delenv("MCP_PROTON_CLIENT_ID")
    assert ident.caller().client_id == "default"
    monkeypatch.setenv("MCP_PROTON_CLIENT_ID", "bad id with spaces")
    with pytest.raises(MailError):
        ident.caller()


def test_http_identity_requires_a_verified_token(app):
    with pytest.raises(MailError) as exc:
        Identity(app, "http").caller()  # no access token in this context
    assert exc.value.code.value == "auth_failed"


async def test_stdio_client_label_is_used_for_policy(make_app, monkeypatch):
    from mcp_proton.domain.families import OperationFamily
    from mcp_proton.policy.model import Action, PolicyRule

    app = make_app(Preset.AUTONOMOUS, rules=[
        PolicyRule(family=OperationFamily.ORGANIZE, action=Action.DENY, client="restricted")])
    monkeypatch.setenv("MCP_PROTON_CLIENT_ID", "restricted")
    async with client_for(app) as c:
        res = await call(c, "mailboxes_create_folder", {"account": ACCOUNT, "name": "F"})
    assert res["error"]["code"] == "policy_denied"


async def test_lifespan_starts_and_stops_watcher_and_closes_app(app, monkeypatch):
    events: list[str] = []

    class FakeWatcher:
        def __init__(self, a):
            assert a is app

        def start(self):
            events.append("start")

        def stop(self, timeout=5.0):
            events.append("stop")

    import mcp_proton.services.events as ev

    monkeypatch.setattr(ev, "Watcher", FakeWatcher)
    monkeypatch.setattr(app, "close", lambda: events.append("close"))
    app.config.watch.mailboxes = ["INBOX"]
    async with client_for(app) as c:
        await c.list_tools()
        assert events == ["start"]
    assert events == ["start", "stop", "close"]


async def test_watcher_failure_does_not_stop_the_server(app, monkeypatch):
    import mcp_proton.services.events as ev

    class Broken:
        def __init__(self, a):
            raise RuntimeError("no idle for you")

    monkeypatch.setattr(ev, "Watcher", Broken)
    app.config.watch.mailboxes = ["INBOX"]
    async with client_for(app) as c:
        assert (await call(c, "accounts_list"))["items"]


async def test_no_watcher_when_no_watch_mailboxes(app, monkeypatch):
    import mcp_proton.services.events as ev

    monkeypatch.setattr(ev, "Watcher", lambda a: pytest.fail("watcher must not start"))
    app.config.watch.mailboxes = []
    async with client_for(app) as c:
        await c.list_tools()


def test_telemetry_is_off_by_default(app, monkeypatch):
    monkeypatch.delenv("FASTMCP_TELEMETRY_MODE", raising=False)
    monkeypatch.setattr(fastmcp.settings, "telemetry_mode", "native")
    build_server(app, transport="stdio")
    assert fastmcp.settings.telemetry_mode == "off"


def test_optional_tool_modules_are_skipped_when_missing(app, monkeypatch):
    monkeypatch.setattr(server_mod, "OPTIONAL_TOOL_MODULES", ["jobs", "does_not_exist"])
    build_server(app, transport="stdio")  # neither module exists yet: no error


async def test_optional_jobs_module_is_picked_up(app, monkeypatch):
    import sys
    import types

    from mcp_proton.mcp.tools.base import Gateway

    mod = types.ModuleType("mcp_proton.mcp.tools.fake_jobs")

    def build(gw: Gateway):
        p = gw.provider()

        @gw.tool(p, "jobs_list", "List jobs.")
        def jobs_list() -> dict:
            return gw.read(lambda c: {"items": [], "client": c.client_id})

        return p

    mod.build = build
    monkeypatch.setitem(sys.modules, "mcp_proton.mcp.tools.fake_jobs", mod)
    monkeypatch.setattr(server_mod, "OPTIONAL_TOOL_MODULES", ["fake_jobs"])
    async with client_for(app) as c:
        assert (await call(c, "jobs_list"))["client"] == "hermes"


async def test_tool_search_is_opt_in(app, monkeypatch):
    async with client_for(app) as c:
        names = {t.name for t in await c.list_tools()}
    assert "messages_move" in names and "search_tools" not in names

    monkeypatch.setenv("MCP_PROTON_TOOL_SEARCH", "1")
    async with Client(build_server(app, transport="stdio")) as c:
        names = {t.name for t in await c.list_tools()}
        assert {"search_tools", "call_tool", "accounts_list"} <= names
        assert "messages_move" not in names
        found = await c.call_tool("search_tools", {"query": "move messages"})
        assert "messages_move" in str(found.content)
