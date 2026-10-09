"""Owner CLI tests. Never touch the real OS keyring."""

from __future__ import annotations

import hashlib
import json

import pytest

from mcp_proton.admin.cli import main
from mcp_proton.bridge import credentials
from mcp_proton.config import load_policy, load_service_config
from mcp_proton.domain.families import OperationFamily
from mcp_proton.domain.requests import CallerContext, OperationRequest, Transport
from mcp_proton.services.core import ExecResult, executor
from mcp_proton.services.factory import build_app

ADDRESS = "alice@proton.example"
SECRET = "demopass"  # noqa: S105 - matches conftest MCP_PROTON_TEST_SECRET


@executor("test.cli_send", OperationFamily.SEND)
def _exec_send(app, rec):  # noqa: ANN001
    return ExecResult(result={"sent_to": rec.request.recipients})


class _Dummy:
    def close(self) -> None:  # store/transport stand-in
        pass


def app_factory(directory):  # noqa: ANN001
    return build_app(directory, store_factory=lambda n: _Dummy(),
                     transport_factory=lambda n: _Dummy())


@pytest.fixture
def keyring_calls(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_store(username: str, secret: str, service: str = "mcp-proton") -> str:
        calls.append((username, secret))
        return f"keyring:{service}/{username}"

    monkeypatch.setattr(credentials, "store_secret_keyring", fake_store)
    return calls


def init_args(*extra: str) -> list[str]:
    return ["init", "--non-interactive", "--name", "main", "--address", ADDRESS,
            "--secret-ref", "env:MCP_PROTON_TEST_SECRET", "--insecure-loopback-plaintext",
            "--skip-connection-test", *extra]


def run(capsys, *argv: str, factory=None) -> tuple[int, str, str]:
    code = main(list(argv), app_factory=factory)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_init_non_interactive(capsys):
    code, out, _ = run(capsys, *init_args("--preset", "assistant", "--storage-mode", "live"))
    assert code == 0
    assert "mcp_proton" not in out and "MCP_PROTON_CLIENT_ID" in out
    cfg = load_service_config()
    assert cfg.accounts[0].name == "main"
    assert cfg.accounts[0].secret_ref == "env:MCP_PROTON_TEST_SECRET"
    assert cfg.accounts[0].imap_security.value == "none"
    assert load_policy().preset.value == "assistant"
    assert SECRET not in (cfg.model_dump_json())


def test_init_without_preset_stays_denied(capsys):
    code, out, _ = run(capsys, *init_args())
    assert code == 0 and "all access remains denied" in out
    assert load_policy().preset is None
    _, out, _ = run(capsys, "policy", "show")
    assert "ALL access is denied" in out


def test_init_autonomous_warns_and_restricts(capsys):
    code, out, _ = run(capsys, *init_args("--preset", "autonomous",
                                          "--restrict-recipients", "bob@x.com,@corp.com"))
    assert code == 0 and "WARNING" in out
    assert load_policy().constraints.allowed_recipients == ["bob@x.com", "@corp.com"]


def test_init_password_stdin_uses_keyring(capsys, monkeypatch, keyring_calls):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("s3cret\n"))
    argv = ["init", "--non-interactive", "--address", ADDRESS, "--password-stdin",
            "--insecure-loopback-plaintext", "--skip-connection-test"]
    code, out, _ = run(capsys, *argv)
    assert code == 0 and keyring_calls == [(f"proton:{ADDRESS}", "s3cret")]
    assert "s3cret" not in out
    assert load_service_config().accounts[0].secret_ref.startswith("keyring:")


def test_init_rejects_plaintext_non_loopback(capsys):
    code, _, err = run(capsys, *init_args("--imap-host", "10.0.0.5"))
    assert code == 1 and "loopback" in err


def test_init_requires_cert_decision(capsys):
    argv = ["init", "--non-interactive", "--address", ADDRESS, "--secret-ref", "env:X",
            "--imap-port", "1", "--skip-connection-test"]
    code, _, err = run(capsys, *argv)
    assert code == 1 and "certificate" in err


def test_init_pins_explicit_fingerprint(capsys):
    argv = ["init", "--non-interactive", "--address", ADDRESS, "--secret-ref", "env:X",
            "--tls-fingerprint", "AA:BB:cc", "--skip-connection-test"]
    assert run(capsys, *argv)[0] == 0
    assert load_service_config().accounts[0].tls_fingerprint_sha256 == "aabbcc"


def test_accounts_lifecycle(capsys):
    run(capsys, *init_args())
    code, out, _ = run(capsys, "accounts", "add", "--non-interactive", "--name", "second",
                       "--address", "b@proton.example", "--secret-ref", "env:X",
                       "--insecure-loopback-plaintext", "--skip-connection-test")
    assert code == 0
    _, out, _ = run(capsys, "accounts", "list", "--json")
    assert [a["name"] for a in json.loads(out)] == ["main", "second"]
    assert ADDRESS not in out
    code, _, err = run(capsys, "accounts", "add", "--non-interactive", "--name", "second",
                       "--address", "b@x", "--secret-ref", "env:X",
                       "--insecure-loopback-plaintext", "--skip-connection-test")
    assert code == 1 and "already exists" in err
    assert run(capsys, "accounts", "remove", "second")[0] == 0
    assert len(load_service_config().accounts) == 1


def test_policy_show_set_unset_grant(capsys):
    run(capsys, *init_args("--preset", "assistant"))
    _, out, _ = run(capsys, "policy", "show", "--json")
    eff = json.loads(out)["effective"]["main"]
    assert eff["send"]["action"] == "ask" and eff["read"]["action"] == "allow"

    run(capsys, "policy", "set", "send", "deny", "--client", "bot")
    _, out, _ = run(capsys, "policy", "show", "--client", "bot", "--json")
    send = json.loads(out)["effective"]["main"]["send"]
    assert send["action"] == "deny" and "client=bot" in send["source"]

    run(capsys, "policy", "set", "send", "allow", "--client", "bot")  # replaces
    assert len(load_policy().rules) == 1
    run(capsys, "policy", "unset", "send", "--client", "bot")
    assert load_policy().rules == []

    code, out, _ = run(capsys, "policy", "grant", "permanent_delete", "--minutes", "10")
    assert code == 0 and "until" in out
    rule = load_policy().rules[0]
    assert rule.action.value == "allow" and rule.expires_at is not None
    _, out, _ = run(capsys, "policy", "show", "--json")
    assert "temporary grant" in json.loads(out)["effective"]["main"]["permanent_delete"]["source"]

    assert run(capsys, "policy", "preset", "reader")[0] == 0
    assert load_policy().preset.value == "reader"


def test_policy_constraints_and_ttl(capsys):
    code, _, _ = run(capsys, "policy", "constraints", "set", "--allowed-recipients",
                     "a@x.com,@corp.com", "--max-batch", "5", "--max-sends-per-day", "10",
                     "--export-dir", "/tmp/out")  # noqa: S108
    assert code == 0
    c = load_policy().constraints
    assert c.allowed_recipients == ["a@x.com", "@corp.com"]
    assert (c.max_batch_size, c.max_sends_per_day) == (5, 10)
    assert c.export_dirs == ["/tmp/out"]  # noqa: S108
    run(capsys, "policy", "constraints", "set", "--allowed-recipients", "none",
        "--max-batch", "none")
    c = load_policy().constraints
    assert c.allowed_recipients is None and c.max_batch_size is None
    assert c.max_sends_per_day == 10
    assert run(capsys, "policy", "approval-ttl", "600")[0] == 0
    assert load_policy().approval_ttl_seconds == 600
    assert run(capsys, "policy", "approval-ttl", "1")[0] == 1


def test_clients_token_shown_once_hash_stored(capsys):
    code, out, _ = run(capsys, "clients", "add", "hermes", "--http-token",
                       "--description", "Hermes agent")
    assert code == 0
    token = out.split("\n  ")[1].strip()
    assert len(token) >= 40
    client = load_policy().client("hermes")
    assert client.token_sha256 == hashlib.sha256(token.encode()).hexdigest()
    assert token not in (config_text := (load_policy().model_dump_json()))
    assert config_text
    _, out, _ = run(capsys, "clients", "list")
    assert token not in out and "hermes" in out

    run(capsys, "clients", "revoke", "hermes")
    assert load_policy().client("hermes").revoked
    run(capsys, "clients", "unrevoke", "hermes")
    assert not load_policy().client("hermes").revoked
    run(capsys, "clients", "review-channel", "hermes", "elicitation")
    assert load_policy().client("hermes").review_channel == "elicitation"
    assert run(capsys, "clients", "revoke", "nope")[0] == 1
    assert run(capsys, "clients", "add", "hermes")[0] == 1


def test_pause_unpause(capsys):
    run(capsys, *init_args())
    run(capsys, "pause", "main")
    assert load_policy().paused_accounts == ["main"]
    run(capsys, "unpause", "main")
    assert load_policy().paused_accounts == []
    assert run(capsys, "pause", "ghost")[0] == 1


def test_approvals_flow_end_to_end(capsys):
    run(capsys, *init_args("--preset", "assistant"))
    app = app_factory(None)
    caller = CallerContext(client_id="agent1", transport=Transport.STDIO)
    req = OperationRequest(kind="test.cli_send", family=OperationFamily.SEND, account="main",
                           recipients=["bob@example.com"], summary="Send hello to Bob",
                           payload={"subject": "Hello", "body": "Hi Bob"})
    out = app.run(caller, req)
    assert out.status.value == "pending"
    op_id = out.operation_id
    app.close()

    _, text, _ = run(capsys, "approvals", "list", factory=app_factory)
    assert op_id in text and "agent1" in text and "Send hello to Bob" in text
    _, text, _ = run(capsys, "approvals", "show", op_id, factory=app_factory)
    assert "bob@example.com" in text and "Hi Bob" in text
    _, text, _ = run(capsys, "approvals", "list", "--json", factory=app_factory)
    assert json.loads(text)[0]["recipients"] == ["bob@example.com"]

    code, text, _ = run(capsys, "approvals", "approve", op_id, "--execute",
                        factory=app_factory)
    assert code == 0 and "succeeded" in text
    _, text, _ = run(capsys, "activity", "--status", "succeeded", "--json",
                     factory=app_factory)
    assert json.loads(text)[0]["id"] == op_id
    _, text, _ = run(capsys, "operations", "show", op_id, factory=app_factory)
    assert "sent_to" in text
    _, text, _ = run(capsys, "approvals", "list", factory=app_factory)
    assert "No pending" in text


def test_approvals_deny_and_error(capsys):
    run(capsys, *init_args("--preset", "assistant"))
    app = app_factory(None)
    req = OperationRequest(kind="test.cli_send", family=OperationFamily.SEND, account="main",
                           recipients=["bob@example.com"], summary="s")
    op_id = app.run(CallerContext(client_id="c"), req).operation_id
    app.close()
    code, text, _ = run(capsys, "approvals", "deny", op_id, factory=app_factory)
    assert code == 0 and "denied" in text
    code, _, err = run(capsys, "approvals", "approve", op_id, factory=app_factory)
    assert code == 1 and "error" in err
    assert run(capsys, "approvals", "show", "op_missing", factory=app_factory)[0] == 1


def test_revoked_client_cannot_resume(capsys):
    run(capsys, *init_args("--preset", "assistant"))
    run(capsys, "clients", "add", "agent1")
    app = app_factory(None)
    req = OperationRequest(kind="test.cli_send", family=OperationFamily.SEND, account="main",
                           recipients=["bob@example.com"], summary="s")
    op_id = app.run(CallerContext(client_id="agent1"), req).operation_id
    app.close()
    run(capsys, "clients", "revoke", "agent1")
    code, _, err = run(capsys, "approvals", "approve", op_id, "--execute", factory=app_factory)
    assert code == 1 and "revoked" in err


def test_purge(capsys):
    run(capsys, *init_args("--preset", "autonomous"))
    app = app_factory(None)
    req = OperationRequest(kind="test.cli_send", family=OperationFamily.SEND, account="main",
                           recipients=["bob@example.com"], summary="s")
    assert app.run(CallerContext(client_id="c"), req).status.value == "succeeded"
    app.close()
    _, text, _ = run(capsys, "purge", "--older-than-days", "30", factory=app_factory)
    assert "Purged 0" in text
    _, text, _ = run(capsys, "purge", "--older-than-days", "0", factory=app_factory)
    assert "Purged 1" in text


def test_client_config(capsys):
    _, out, _ = run(capsys, "client-config", "claude-desktop", "--client-id", "cd")
    data = json.loads(out)["mcpServers"]["proton"]
    assert data["env"]["MCP_PROTON_CLIENT_ID"] == "cd" and data["args"][0] == "serve"
    _, out, _ = run(capsys, "client-config", "hermes", "--client-id", "hermes")
    assert "mcp_servers:" in out and "MCP_PROTON_CLIENT_ID" in out and "Example" in out
    _, out, _ = run(capsys, "client-config", "generic", "--http", "http://127.0.0.1:8765/mcp")
    assert "Bearer" in json.loads(out)["headers"]["Authorization"]


def test_serve_without_mcp_layer_reports_cleanly(capsys, monkeypatch):
    import builtins

    real = builtins.__import__

    def fake(name, *a, **k):
        if name.endswith("mcp.server"):
            raise ImportError("no server")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    code, _, err = run(capsys, "serve", factory=app_factory)
    assert code == 1 and "not available" in err
