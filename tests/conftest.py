"""Shared fixtures.

* ``imap_server``: a disposable pymap (in-memory) IMAP server shaped like a
  Proton Bridge mailbox tree. It is a *fake*: passing tests against it is not
  Bridge compatibility evidence (see docs/compatibility.md).
* ``smtp_server``: an aiosmtpd server recording submitted messages, with
  hooks to reject recipients or drop the connection after DATA.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field

import pytest

BRIDGE_TREE = ["Archive", "Drafts", "Sent", "Spam", "Trash", "All Mail", "Starred",
               "Folders", "Labels"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class ImapServer:
    host: str
    port: int
    user: str = "demouser"
    password: str = "demopass"  # noqa: S105 - fake test server credential


@pytest.fixture
def imap_server():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; from pymap.main import main; sys.exit(main())", "--port", str(port), "--host", "127.0.0.1",
         "--no-service", "managesieve", "--no-service", "admin", "dict"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.skip("pymap did not start")
    from imapclient import IMAPClient

    c = IMAPClient("127.0.0.1", port=port, ssl=False)
    c.login("demouser", "demopass")
    for name in BRIDGE_TREE:
        if not c.folder_exists(name):
            c.create_folder(name)
    c.logout()
    yield ImapServer("127.0.0.1", port)
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()


@dataclass
class SmtpServer:
    host: str
    port: int
    messages: list = field(default_factory=list)  # (mail_from, rcpt_tos, data)
    reject: set = field(default_factory=set)  # recipient addresses to reject with 550
    drop_after_data: bool = False  # simulate connection loss after DATA accepted


@pytest.fixture
def smtp_server():
    from aiosmtpd.controller import Controller

    port = _free_port()
    state = SmtpServer("127.0.0.1", port)

    class Handler:
        async def handle_RCPT(self, server, session, envelope, address, rcpt_options):  # noqa: N802
            if address in state.reject:
                return "550 5.1.1 recipient rejected"
            envelope.rcpt_tos.append(address)
            return "250 OK"

        async def handle_DATA(self, server, session, envelope):  # noqa: N802
            state.messages.append((envelope.mail_from, list(envelope.rcpt_tos), envelope.content))
            if state.drop_after_data:
                server.transport.close()
                return "250 OK"
            return "250 OK queued"

    ctl = Controller(Handler(), hostname="127.0.0.1", port=port,
                     auth_require_tls=False, auth_required=False)
    ctl.start()
    yield state
    ctl.stop()


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_PROTON_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MCP_PROTON_TEST_SECRET", "demopass")
    os.environ.pop("MCP_PROTON_LIVE", None) if os.environ.get("MCP_PROTON_LIVE") != "1" else None
