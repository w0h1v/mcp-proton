"""Diagnostics must stay metadata-only."""

from __future__ import annotations

import json

from mcp_proton.admin import diagnostics
from mcp_proton.admin.cli import main
from mcp_proton.config import AccountConfig, ServiceConfig, StorageMode
from mcp_proton.policy.model import PolicyConfig, Preset

ADDRESS = "alice@proton.example"


def init(capsys) -> None:
    main(["init", "--non-interactive", "--name", "main", "--address", ADDRESS,
          "--username", "alice-bridge-user", "--secret-ref", "env:MCP_PROTON_TEST_SECRET",
          "--insecure-loopback-plaintext", "--skip-connection-test", "--preset", "reader"])
    capsys.readouterr()


def test_cli_diagnostics_has_no_secrets_or_addresses(capsys):
    init(capsys)
    for flags in ([], ["--json"]):
        assert main(["diagnostics", "--no-probe", *flags]) == 0
        out = capsys.readouterr().out
        assert "main" in out and "reader" in out
        for secret in (ADDRESS, "alice", "demopass", "MCP_PROTON_TEST_SECRET"):
            assert secret not in out


def test_cli_diagnostics_json_is_valid(capsys):
    init(capsys)
    main(["diagnostics", "--json", "--no-probe"])
    data = json.loads(capsys.readouterr().out)
    assert data["versions"]["python"] and data["config"]["accounts"][0]["secret_provider"] == "env"


def test_probe_reduces_errors_and_hides_mailboxes():
    class FakeReport:
        server_capabilities = ["IMAP4rev1", "IDLE"]
        features = {"idle": "available"}
        special_folders = {"sent": "Sent Items Secret"}
        server_id = {"name": "Bridge"}
        delimiter = "/"

    class FakeStore:
        closed = False

        def capabilities(self):
            return FakeReport()

        def close(self):
            FakeStore.closed = True

    acct = AccountConfig(name="a", address=ADDRESS, username=ADDRESS, secret_ref="env:X")
    cfg = ServiceConfig(accounts=[acct], storage_mode=StorageMode.LIVE)
    pol = PolicyConfig(preset=Preset.READER)
    rep = diagnostics.collect(cfg, pol, store_factory=lambda a: FakeStore())
    text = json.dumps(rep)
    assert "Secret" not in text and ADDRESS not in text and FakeStore.closed
    rep = diagnostics.collect(cfg, pol, include_mailboxes=True,
                              store_factory=lambda a: FakeStore())
    assert "Sent Items Secret" in json.dumps(rep)

    def boom(a):
        raise RuntimeError(f"cannot reach {ADDRESS}")

    rep = diagnostics.collect(cfg, pol, store_factory=boom)
    assert ADDRESS not in json.dumps(rep)
    assert rep["config"]["accounts"][0]["capabilities"]["error"] == "RuntimeError"
