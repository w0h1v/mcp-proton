"""Resources use the same identity and policy checks as tools."""

from __future__ import annotations

import json

import pytest
from mcp.shared.exceptions import MCPError

from conftest import ACCOUNT, call, client_for  # type: ignore[import-not-found]
from mcp_proton.policy.model import Preset


async def read(c, uri):
    parts = await c.read_resource(uri)
    return json.loads(parts[0].text)


async def test_resource_templates_use_opaque_uris(app):
    async with client_for(app) as c:
        templates = {t.uri_template for t in await c.list_resource_templates()}
    assert templates == {
        "proton://capabilities/{account}", "proton://mailboxes/{account}",
        "proton://operations/{operation_id}", "proton://messages/{handle}",
    }


async def test_reader_can_read_resources(make_app, seed):
    app = make_app(Preset.READER)
    [h] = seed(subject="Invoice 42")
    async with client_for(app) as c:
        caps = await read(c, f"proton://capabilities/{ACCOUNT}")
        assert "inventory" in caps
        boxes = await read(c, f"proton://mailboxes/{ACCOUNT}")
        assert any(m["name"] == "INBOX" for m in boxes["items"])
        msg = await read(c, f"proton://messages/{h}")
        assert msg["message"]["subject"] == "Invoice 42"
        assert "Invoice" not in f"proton://messages/{h}"  # URI is opaque


async def test_not_onboarded_resources_denied(make_app, seed):
    app = make_app(None)
    async with client_for(app) as c:
        with pytest.raises(MCPError) as exc:
            await c.read_resource(f"proton://capabilities/{ACCOUNT}")
    assert json.loads(str(exc.value))["code"] == "not_onboarded"


async def test_revoked_client_resource_denied(make_app, seed):
    from mcp_proton.policy.model import ClientConfig

    app = make_app(Preset.AUTONOMOUS, clients=[ClientConfig(client_id="hermes", revoked=True)])
    async with client_for(app) as c:
        with pytest.raises(MCPError) as exc:
            await c.read_resource(f"proton://mailboxes/{ACCOUNT}")
    assert json.loads(str(exc.value))["code"] == "client_revoked"


async def test_message_resource_respects_body_release(make_app, seed):
    from mcp_proton.policy.model import Constraints

    app = make_app(Preset.READER, constraints=Constraints(release_bodies=False))
    [h] = seed(body="secret body text")
    async with client_for(app) as c:
        msg = await read(c, f"proton://messages/{h}")
    assert "secret body text" not in json.dumps(msg)


async def test_operation_resource_is_caller_scoped(make_app, monkeypatch):
    app = make_app(Preset.ASSISTANT)
    async with client_for(app) as c:
        out = await call(c, "mail_send", {"account": ACCOUNT, "to": ["bob@example.com"],
                                          "subject": "x", "text": "y"})
        status = await read(c, f"proton://operations/{out['operation_id']}")
        assert status["status"] == "pending"
    monkeypatch.setenv("MCP_PROTON_CLIENT_ID", "someone-else")
    async with client_for(app) as c:
        with pytest.raises(MCPError) as exc:
            await c.read_resource(f"proton://operations/{out['operation_id']}")
    assert json.loads(str(exc.value))["code"] == "not_found"
