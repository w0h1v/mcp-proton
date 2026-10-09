"""A MailApp wired to the real IMAP/SMTP adapters against fake servers."""

from __future__ import annotations

import pytest

from mcp_proton.config import AccountConfig, Security, ServiceConfig
from mcp_proton.domain.requests import CallerContext
from mcp_proton.policy.model import Constraints, PolicyConfig, Preset
from mcp_proton.services.core import MailApp
from mcp_proton.storage.db import Database

ACCOUNT = "t"
ADDRESS = "demouser@x.test"


@pytest.fixture
def account_cfg(imap_server, smtp_server, tmp_path):
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

    def _make(preset: Preset | None = Preset.AUTONOMOUS, **policy_kw) -> MailApp:
        policy_kw.setdefault("constraints", Constraints(
            attachment_ingest_dirs=[str(tmp_path / "ingest")],
            export_dirs=[str(tmp_path / "export")],
            import_dirs=[str(tmp_path / "import")],
        ))
        for d in ("ingest", "export", "import"):
            (tmp_path / d).mkdir(exist_ok=True)
        cfg = ServiceConfig(accounts=[account_cfg], data_dir=str(tmp_path / "data"))
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
def caller():
    return CallerContext(client_id="hermes")


@pytest.fixture
def owner():
    return CallerContext.owner()


def make_raw(subject="Hello", frm="alice@example.com", to=ADDRESS, body="Hi there",
             message_id=None, extra_headers="") -> bytes:
    import secrets

    mid = message_id or f"<{secrets.token_hex(8)}@example.com>"
    return (
        f"From: Alice <{frm}>\r\nTo: {to}\r\nSubject: {subject}\r\n"
        f"Date: Thu, 08 Oct 2026 10:00:00 +0000\r\nMessage-ID: {mid}\r\n{extra_headers}"
        f"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n"
    ).encode()


@pytest.fixture
def seed(app):
    """seed(mailbox, n=1, **make_raw kwargs) -> list[handle tokens] via the adapter."""
    from mcp_proton.domain.models import MessageHandle

    def _seed(mailbox="INBOX", n=1, flags=None, **kw):
        store = app.store(ACCOUNT)
        out = []
        for i in range(n):
            kw2 = dict(kw)
            kw2.setdefault("subject", f"Message {i}")
            r = store.append(mailbox, make_raw(**kw2), flags=flags)
            out.append(MessageHandle(account=ACCOUNT, mailbox=mailbox,
                                     uidvalidity=r.uidvalidity, uid=r.uid).token())
        return out

    return _seed
