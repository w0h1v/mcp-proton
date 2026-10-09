"""Fixtures for MCP-layer tests: a MailApp over pymap + aiosmtpd, wrapped by FastMCP.

``tests/mcp`` deliberately has no ``__init__.py``: a package named ``mcp`` here
would shadow the MCP SDK that FastMCP imports.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import pytest
from fastmcp import Client

from mcp_proton.config import AccountConfig, Security, ServiceConfig
from mcp_proton.domain.models import MessageHandle
from mcp_proton.domain.requests import CallerContext
from mcp_proton.mcp.server import build_server
from mcp_proton.policy.model import ClientConfig, Constraints, PolicyConfig, Preset
from mcp_proton.services.core import MailApp
from mcp_proton.storage.db import Database

ACCOUNT = "t"
ADDRESS = "demouser@x.test"


def make_raw(subject="Hello", frm="alice@example.com", to=ADDRESS, body="Hi there",
             message_id=None) -> bytes:
    mid = message_id or f"<{secrets.token_hex(8)}@example.com>"
    return (
        f"From: Alice <{frm}>\r\nTo: {to}\r\nSubject: {subject}\r\n"
        f"Date: Thu, 08 Oct 2026 10:00:00 +0000\r\nMessage-ID: {mid}\r\n"
        f"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n"
    ).encode()


@pytest.fixture
def account_cfg(imap_server, smtp_server):
    return AccountConfig(
        name=ACCOUNT, address=ADDRESS, username=imap_server.user,
        secret_ref="env:MCP_PROTON_TEST_SECRET",
        imap_host=imap_server.host, imap_port=imap_server.port, imap_security=Security.NONE,
        smtp_host=smtp_server.host, smtp_port=smtp_server.port, smtp_security=Security.NONE,
        identities=["alias@x.test"],
    )


@pytest.fixture
def make_app(account_cfg, tmp_path):
    from mcp_proton.bridge.imap import open_store
    from mcp_proton.bridge.smtp import open_transport

    apps: list[MailApp] = []

    def _make(preset: Preset | None = Preset.AUTONOMOUS, **policy_kw: Any) -> MailApp:
        policy_kw.setdefault("constraints", Constraints(
            export_dirs=[str(tmp_path / "export")], import_dirs=[str(tmp_path / "import")],
        ))
        for d in ("export", "import"):
            (tmp_path / d).mkdir(exist_ok=True)
        cfg = ServiceConfig(accounts=[account_cfg], data_dir=str(tmp_path / "data"))
        cfg.watch.mailboxes = []  # no background watcher in tests
        app = MailApp(cfg, PolicyConfig(preset=preset, **policy_kw),
                      Database(tmp_path / f"db{len(apps)}.sqlite3"),
                      store_factory=lambda name: open_store(cfg.account(name)),
                      transport_factory=lambda name: open_transport(cfg.account(name)))
        apps.append(app)
        return app

    yield _make
    for a in apps:
        a.close()


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def owner():
    return CallerContext.owner()


@pytest.fixture(autouse=True)
def _client_label(monkeypatch):
    monkeypatch.setenv("MCP_PROTON_CLIENT_ID", "hermes")


@pytest.fixture
def seed(account_cfg):
    """seed(mailbox, n=1, **make_raw kwargs) -> handle tokens (straight through the adapter)."""
    from mcp_proton.bridge.imap import open_store

    store = open_store(account_cfg)

    def _seed(mailbox="INBOX", n=1, flags=None, **kw):
        out = []
        for i in range(n):
            kw2 = dict(kw)
            kw2.setdefault("subject", f"Message {i}")
            r = store.append(mailbox, make_raw(**kw2), flags=flags)
            out.append(MessageHandle(account=ACCOUNT, mailbox=mailbox,
                                     uidvalidity=r.uidvalidity, uid=r.uid).token())
        return out

    yield _seed
    store.close()


def client_for(app: MailApp, **kw: Any) -> Client:
    """In-memory client on a stdio-equivalent server."""
    return Client(build_server(app, transport="stdio"), **kw)


async def call(c: Client, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a tool; return structured content, or ``{"error": {...}}`` for tool errors."""
    res = await c.call_tool(name, args or {}, raise_on_error=False)
    if res.is_error:
        text = res.content[0].text  # type: ignore[union-attr]
        try:
            return {"error": json.loads(text)}
        except ValueError:
            return {"error": {"raw": text}}
    return res.structured_content or {}


def policy_with_client(client: ClientConfig, preset: Preset = Preset.ASSISTANT) -> dict[str, Any]:
    return {"preset": preset, "clients": [client]}
