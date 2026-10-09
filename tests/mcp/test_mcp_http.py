"""HTTP transport: bearer authentication against policy clients, Origin checks."""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager

import httpx2
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp.shared.exceptions import MCPError

from conftest import ACCOUNT, call  # type: ignore[import-not-found]
from mcp_proton.domain.errors import MailError
from mcp_proton.mcp.auth import match_client
from mcp_proton.mcp.server import build_http_app, build_server
from mcp_proton.policy.model import ClientConfig, Preset

TOKEN = "tok-hermes-0123456789"  # noqa: S105 - test credential
OTHER = "tok-other-9876543210"  # noqa: S105
URL = "http://127.0.0.1:8765/mcp"


def sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture
def http_app(make_app):
    return make_app(Preset.AUTONOMOUS, clients=[
        ClientConfig(client_id="hermes", token_sha256=sha(TOKEN)),
        ClientConfig(client_id="other", token_sha256=sha(OTHER)),
        ClientConfig(client_id="label-only"),
    ])


@asynccontextmanager
async def serve(app, **client_kw):
    """Yield (asgi_app, factory) with the ASGI lifespan running."""
    asgi = build_http_app(app)

    def factory(**kw):
        kw.pop("transport", None)
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=asgi), **kw)

    async with asgi.router.lifespan_context(asgi):
        yield asgi, factory


def mcp_client(factory, token: str | None, **kw) -> Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return Client(StreamableHttpTransport(URL, headers=headers, httpx_client_factory=factory),
                  **kw)


async def test_valid_token_authenticates_and_binds_identity(http_app, seed):
    [h] = seed(subject="over http")
    async with serve(http_app) as (_, factory), mcp_client(factory, TOKEN) as c:
        tools = await c.list_tools()
        assert any(t.name == "messages_list" for t in tools)
        got = await call(c, "messages_get", {"handle": h})
        assert got["message"]["subject"] == "over http"
        out = await call(c, "messages_trash", {"handles": [h]})
        assert out["status"] == "succeeded"
    rec = http_app.journal.list(limit=1)[0]
    assert rec.client_id == "hermes" and rec.transport == "http"


async def test_identity_comes_from_token_not_self_reported_name(http_app, monkeypatch):
    monkeypatch.setenv("MCP_PROTON_CLIENT_ID", "spoofed")  # env is stdio-only
    async with serve(http_app) as (_, factory), mcp_client(factory, OTHER, name="hermes") as c:
        out = await call(c, "mailboxes_create_folder", {"account": ACCOUNT, "name": "F1"})
    assert out["status"] == "succeeded"
    assert http_app.journal.list(limit=1)[0].client_id == "other"


async def test_missing_and_invalid_tokens_get_401(http_app):
    async with serve(http_app) as (asgi, _):
        transport = httpx2.ASGITransport(app=asgi)
        async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as h:
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            accept = {"Accept": "application/json, text/event-stream"}
            r = await h.post("/mcp", json=body, headers=accept)
            assert r.status_code == 401
            r = await h.post("/mcp", json=body, headers={**accept, "Authorization": "Bearer nope"})
            assert r.status_code == 401
            stored_hash = {**accept, "Authorization": f"Bearer {sha(TOKEN)}"}
            r = await h.post("/mcp", json=body, headers=stored_hash)
            assert r.status_code == 401  # the stored hash is not a credential


async def test_client_without_token_cannot_authenticate(http_app):
    async with serve(http_app) as (asgi, _):
        transport = httpx2.ASGITransport(app=asgi)
        async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as h:
            r = await h.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                             headers={"Accept": "application/json, text/event-stream",
                                      "Authorization": "Bearer label-only"})
    assert r.status_code == 401


async def test_revoked_client_is_refused_immediately(http_app):
    async with serve(http_app) as (_, factory):
        async with mcp_client(factory, TOKEN) as c:
            assert (await call(c, "accounts_list"))["items"]
            # owner revokes while the session is live: the very next call is refused
            http_app.set_policy(http_app.policy.model_copy(update={"clients": [
                ClientConfig(client_id="hermes", token_sha256=sha(TOKEN), revoked=True),
                ClientConfig(client_id="other", token_sha256=sha(OTHER)),
            ]}))
            with pytest.raises(MCPError):  # the transport answers 401 to the live session
                await call(c, "accounts_list")
        with pytest.raises(Exception):  # noqa: B017, PT011 - new connections get 401 too
            async with mcp_client(factory, TOKEN) as c2:
                await c2.list_tools()


async def test_rotated_token_stops_working(http_app):
    async with serve(http_app) as (_, factory), mcp_client(factory, TOKEN) as c:
        assert (await call(c, "accounts_list"))["items"]
        http_app.set_policy(http_app.policy.model_copy(update={"clients": [
            ClientConfig(client_id="hermes", token_sha256=sha("brand-new-token")),
            ClientConfig(client_id="other", token_sha256=sha(OTHER)),
        ]}))
        with pytest.raises(MCPError):
            await call(c, "accounts_list")


async def test_origin_header_rejected_by_default(http_app):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    base = {"Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {TOKEN}"}
    async with serve(http_app) as (asgi, _):
        transport = httpx2.ASGITransport(app=asgi)
        async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as h:
            for origin in ("https://evil.example", "http://localhost:3000",
                           "http://127.0.0.1:8765"):
                r = await h.post("/mcp", json=body, headers={**base, "Origin": origin})
                assert r.status_code == 403, origin


async def test_allowed_origin_passes_the_guard(http_app):
    http_app.config.http.allowed_origins = ["https://ui.example"]
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    async with serve(http_app) as (asgi, _):
        transport = httpx2.ASGITransport(app=asgi)
        async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as h:
            ok = await h.post("/mcp", json=body, headers={
                "Accept": "application/json, text/event-stream", "Origin": "https://ui.example"})
            assert ok.status_code == 401  # past the origin guard, stopped by auth
            bad = await h.post("/mcp", json=body, headers={
                "Accept": "application/json, text/event-stream", "Origin": "https://evil.example"})
            assert bad.status_code == 403


def test_http_refuses_to_start_without_token_clients(make_app):
    app = make_app(Preset.AUTONOMOUS, clients=[ClientConfig(client_id="label-only")])
    with pytest.raises(MailError) as exc:
        build_server(app, transport="http")
    assert "bearer token" in exc.value.message
    revoked = make_app(Preset.AUTONOMOUS, clients=[
        ClientConfig(client_id="x", token_sha256=sha(TOKEN), revoked=True)])
    with pytest.raises(MailError):
        build_server(revoked, transport="http")
    none_at_all = make_app(Preset.AUTONOMOUS)
    with pytest.raises(MailError):
        build_server(none_at_all, transport="http")


def test_match_client_is_exact(http_app):
    pol = http_app.policy
    assert match_client(pol, TOKEN).client_id == "hermes"
    assert match_client(pol, TOKEN + "x") is None
    assert match_client(pol, "") is None


def test_run_server_binds_configured_host(http_app, monkeypatch):
    from fastmcp import FastMCP

    from mcp_proton.mcp.server import run_server

    seen = {}
    monkeypatch.setattr(FastMCP, "run", lambda self, **kw: seen.update(kw))
    run_server(http_app, transport="http")
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8765
    assert seen["transport"] == "http" and seen["allowed_origins"] == []
    http_app.config.http.host = "10.1.2.3"
    run_server(http_app, transport="http", port=9000)
    assert seen["host"] == "10.1.2.3" and seen["port"] == 9000
