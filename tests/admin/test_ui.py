from __future__ import annotations

import hashlib
import re

import pytest
from starlette.testclient import TestClient

from mcp_proton.admin import ui as ui_mod
from mcp_proton.admin.ui import UiSetupError, build_ui, issue_owner_token
from mcp_proton.config import (
    AccountConfig,
    Security,
    ServiceConfig,
    load_policy,
    load_service_config,
    save_policy,
)
from mcp_proton.domain.models import Address, OperationStatus, OutgoingMessage
from mcp_proton.domain.requests import CallerContext
from mcp_proton.policy.model import ClientConfig, PolicyConfig, Preset
from mcp_proton.services import sending
from mcp_proton.services.core import MailApp
from mcp_proton.services.factory import make_policy_loader
from mcp_proton.storage.db import Database

ORIGIN = "http://127.0.0.1:8766"
TOKEN = "owner-secret-token"  # noqa: S105
EVIL = "<script>alert(1)</script>"


def _no_store(name: str):
    raise AssertionError("the UI tests must not open mail connections")


@pytest.fixture
def cfg_dir(tmp_path):
    d = tmp_path / "uicfg"
    save_policy(PolicyConfig(preset=Preset.ASSISTANT,
                             clients=[ClientConfig(client_id="hermes", description="agent <b>")]),
                d)
    return d


@pytest.fixture
def app(tmp_path, cfg_dir):
    acct = AccountConfig(name="t", address="me@x.test", username="me", secret_ref="env:X",
                         imap_security=Security.NONE, smtp_security=Security.NONE)
    cfg = ServiceConfig(accounts=[acct], data_dir=str(tmp_path / "data"),
                        owner_token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest())
    loader = make_policy_loader(cfg_dir)
    a = MailApp(cfg, loader(), Database(tmp_path / "ui.sqlite3"), _no_store, _no_store,
                policy_loader=loader)
    yield a
    a.db.close()


@pytest.fixture
def client(app, cfg_dir):
    return TestClient(build_ui(app, config_dir_=cfg_dir), base_url=ORIGIN,
                      follow_redirects=False)


def _login(c: TestClient, token: str = TOKEN):
    return c.post("/login", data={"token": token}, headers={"Origin": ORIGIN})


@pytest.fixture
def authed(client):
    assert _login(client).status_code == 303
    return client


def _csrf(c: TestClient, path: str = "/accounts") -> str:
    r = c.get(path)
    assert r.status_code == 200
    return re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)


def _post(c, path, data, csrf=True, origin=ORIGIN, from_page="/accounts"):
    data = dict(data)
    if csrf is True:
        data["csrf"] = _csrf(c, from_page)
    elif csrf:
        data["csrf"] = csrf
    headers = {"Origin": origin} if origin else {}
    return c.post(path, data=data, headers=headers)


def _pending(app, subject=EVIL, text="<img src=x onerror=alert(1)>", html="<p>h</p>"):
    out = sending.send(app, CallerContext(client_id="hermes"), OutgoingMessage(
        account="t", to=[Address(name="<i>Bob</i>", email="bob@example.com")],
        subject=subject, text=text, html=html))
    assert out.status is OperationStatus.PENDING
    return out.operation_id


# ------------------------------------------------------------------ startup / auth


def test_refuses_without_owner_token(app):
    app.config.owner_token_sha256 = None
    with pytest.raises(UiSetupError, match="ui-token"):
        build_ui(app)


def test_issue_owner_token_stores_only_hash(tmp_path):
    d = tmp_path / "cfg"
    token = issue_owner_token(d)
    stored = load_service_config(d).owner_token_sha256
    assert stored == hashlib.sha256(token.encode()).hexdigest() and token not in stored
    assert issue_owner_token(d) != token


@pytest.mark.parametrize("path", ["/", "/accounts", "/access", "/activity", "/reviews",
                                  "/jobs", "/storage", "/operations/op_x"])
def test_unauthenticated_requests_redirect_to_login(client, path):
    r = client.get(path)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "mcp-proton" not in r.text


def test_unauthenticated_post_is_rejected(client, app):
    r = client.post("/accounts/pause", data={"account": "t", "paused": "1"},
                    headers={"Origin": ORIGIN})
    assert r.status_code == 401
    assert "t" not in app.policy.paused_accounts


def test_wrong_token_fails_and_right_token_works(client):
    r = _login(client, "nope")
    assert r.status_code == 401 and "set-cookie" not in r.headers
    assert client.get("/accounts").status_code == 303
    r = _login(client)
    assert r.status_code == 303 and r.headers["location"] == "/accounts"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" not in cookie
    page = client.get("/accounts")
    assert page.status_code == 200 and "Accounts" in page.text


def test_login_requires_same_origin(client):
    assert client.post("/login", data={"token": TOKEN}).status_code == 403
    assert client.post("/login", data={"token": TOKEN},
                       headers={"Origin": "http://evil.example"}).status_code == 403


def test_cookie_secure_over_https(app, cfg_dir):
    c = TestClient(build_ui(app, config_dir_=cfg_dir), base_url="https://127.0.0.1:8766",
                   follow_redirects=False)
    r = c.post("/login", data={"token": TOKEN}, headers={"Origin": "https://127.0.0.1:8766"})
    assert "secure" in r.headers["set-cookie"].lower()


def test_login_is_rate_limited(client):
    for _ in range(ui_mod.LOGIN_MAX_FAILURES):
        assert _login(client, "bad").status_code == 401
    assert _login(client).status_code == 429  # even the right token waits


def test_logout_ends_session(authed):
    csrf = _csrf(authed)
    r = authed.post("/logout", data={"csrf": csrf}, headers={"Origin": ORIGIN})
    assert r.status_code == 303
    assert authed.get("/accounts").status_code == 303


def test_host_header_must_be_loopback(authed):
    assert authed.get("/accounts", headers={"Host": "evil.example"}).status_code == 400
    assert authed.get("/accounts", headers={"Host": "localhost:8766"}).status_code == 200


def test_security_headers_everywhere(client, authed):
    for r in (authed.get("/accounts"), authed.get("/nope"), authed.get("/", ),
              authed.get("/accounts", headers={"Host": "evil.example"})):
        assert r.headers["content-security-policy"] == (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'")
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["cache-control"] == "no-store"
    fresh = TestClient(build_ui(authed.app.state.ui.app, config_dir_=authed.app.state.ui.dir),
                       base_url=ORIGIN, follow_redirects=False)
    assert fresh.get("/login").headers["x-frame-options"] == "DENY"


def test_pages_have_no_scripts_or_external_assets(authed):
    for path in ("/accounts", "/access", "/activity", "/reviews", "/jobs", "/storage"):
        text = authed.get(path).text
        assert "<script" not in text.lower()
        assert not re.search(r'(src|href)="https?://', text)


# ------------------------------------------------------------------ pending reviews


def test_pending_review_shows_exact_payload_escaped(authed, app):
    op = _pending(app)
    page = authed.get("/reviews")
    assert page.status_code == 200
    assert op in page.text
    assert EVIL not in page.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert "<img src=x" not in page.text and "&lt;img src=x onerror=alert(1)&gt;" in page.text
    assert "&lt;i&gt;Bob&lt;/i&gt; &lt;bob@example.com&gt;" in page.text
    assert "bob@example.com" in page.text and "Approve" in page.text and "Deny" in page.text
    assert "deny it and ask the agent" in page.text  # edit hint
    detail = authed.get(f"/operations/{op}")
    assert detail.status_code == 200 and EVIL not in detail.text
    assert authed.get("/operations/op_missing").status_code == 404


def test_approve_requires_csrf(authed, app):
    op = _pending(app)
    form = {"op_id": op, "decision": "approve",
            "digest": app.journal.get(op).digest}
    for csrf in (False, "wrong-token"):
        r = _post(authed, "/operations/decide", form, csrf=csrf, from_page="/reviews")
        assert r.status_code == 403
    assert app.journal.get(op).status is OperationStatus.PENDING


def test_approve_requires_same_origin(authed, app):
    op = _pending(app)
    form = {"op_id": op, "decision": "approve", "digest": app.journal.get(op).digest}
    for origin in ("http://evil.example", "http://127.0.0.1:9999", None):
        r = _post(authed, "/operations/decide", form, origin=origin, from_page="/reviews")
        assert r.status_code == 403
    # a matching Referer is accepted when Origin is absent
    csrf = _csrf(authed, "/reviews")
    r = authed.post("/operations/decide", data={**form, "csrf": csrf},
                    headers={"Referer": f"{ORIGIN}/reviews"})
    assert r.status_code == 303
    assert app.journal.get(op).status is OperationStatus.APPROVED


def test_approve_and_deny_use_the_owner_services(authed, app):
    a, d = _pending(app, subject="one"), _pending(app, subject="two")
    r = _post(authed, "/operations/decide",
              {"op_id": a, "decision": "approve", "digest": app.journal.get(a).digest},
              from_page="/reviews")
    assert r.status_code == 303 and r.headers["location"] == "/reviews"
    rec = app.journal.get(a)
    assert rec.status is OperationStatus.APPROVED and rec.decided_by == "owner:ui"
    _post(authed, "/operations/decide",
          {"op_id": d, "decision": "deny", "digest": app.journal.get(d).digest},
          from_page="/reviews")
    assert app.journal.get(d).status is OperationStatus.DENIED
    page = authed.get("/reviews")
    assert "No pending reviews" in page.text and "waiting for the agent to resume" in page.text


def test_decision_with_stale_digest_is_refused(authed, app):
    op = _pending(app)
    r = _post(authed, "/operations/decide",
              {"op_id": op, "decision": "approve", "digest": "0" * 64}, from_page="/reviews")
    assert r.status_code == 303
    assert app.journal.get(op).status is OperationStatus.PENDING
    assert "differs from what was displayed" in authed.get("/reviews").text


def test_decide_unknown_op_shows_error(authed):
    r = _post(authed, "/operations/decide",
              {"op_id": "op_nope", "decision": "approve", "digest": "x"}, from_page="/reviews")
    assert r.status_code == 303
    assert "not_found" in authed.get("/reviews").text


def test_post_needs_urlencoded_form(authed):
    csrf = _csrf(authed)
    r = authed.post("/accounts/pause", json={"csrf": csrf}, headers={"Origin": ORIGIN})
    assert r.status_code == 400


# ------------------------------------------------------------------ policy edits


def test_revoke_and_restore_client_updates_policy_file(authed, app, cfg_dir):
    r = _post(authed, "/access/client", {"client": "hermes", "revoked": "1"}, from_page="/access")
    assert r.status_code == 303
    assert load_policy(cfg_dir).client("hermes").revoked is True
    assert app.policy.client("hermes").revoked is True
    page = authed.get("/access")
    assert "revoked" in page.text and "Restore" in page.text
    _post(authed, "/access/client", {"client": "hermes", "revoked": "0"}, from_page="/access")
    assert load_policy(cfg_dir).client("hermes").revoked is False


def test_revoked_client_cannot_read_after_ui_revoke(authed, app):
    from mcp_proton.domain.errors import ErrorCode, MailError

    _post(authed, "/access/client", {"client": "hermes", "revoked": "1"}, from_page="/access")
    with pytest.raises(MailError) as e:
        app.authorize_read(CallerContext(client_id="hermes"), "t")
    assert e.value.code is ErrorCode.CLIENT_REVOKED


def test_unknown_client_is_rejected(authed, cfg_dir):
    _post(authed, "/access/client", {"client": "ghost", "revoked": "1"}, from_page="/access")
    assert load_policy(cfg_dir).client("ghost") is None
    assert "Unknown client" in authed.get("/access").text


def test_pause_and_resume_account(authed, app, cfg_dir):
    _post(authed, "/accounts/pause", {"account": "t", "paused": "1"})
    assert load_policy(cfg_dir).paused_accounts == ["t"] and "Resume" in authed.get(
        "/accounts").text
    _post(authed, "/accounts/pause", {"account": "t", "paused": "0"})
    assert load_policy(cfg_dir).paused_accounts == []
    _post(authed, "/accounts/pause", {"account": "ghost", "paused": "1"})
    assert load_policy(cfg_dir).paused_accounts == []
    assert "not_found" in authed.get("/accounts").text


def test_preset_change_and_autonomous_confirmation(authed, cfg_dir):
    _post(authed, "/access/preset", {"preset": "reader"}, from_page="/access")
    assert load_policy(cfg_dir).preset is Preset.READER
    _post(authed, "/access/preset", {"preset": "autonomous"}, from_page="/access")
    assert load_policy(cfg_dir).preset is Preset.READER  # needs the confirmation box
    _post(authed, "/access/preset", {"preset": "autonomous", "confirm": "1"}, from_page="/access")
    assert load_policy(cfg_dir).preset is Preset.AUTONOMOUS
    _post(authed, "/access/preset", {"preset": "bogus"}, from_page="/access")
    assert load_policy(cfg_dir).preset is Preset.AUTONOMOUS


def test_access_page_shows_effective_actions(authed):
    page = authed.get("/access?client=hermes")
    assert "Effective access for client" in page.text
    for fam in ("read", "send", "permanent_delete"):
        assert f"<td>{fam}</td>" in page.text
    assert "preset assistant" in page.text
    assert "agent &lt;b&gt;" in page.text  # client description escaped


# ------------------------------------------------------------------ other pages


def test_activity_lists_operations_and_filters(authed, app):
    op = _pending(app)
    page = authed.get("/activity")
    assert op in page.text and EVIL not in page.text
    assert op not in authed.get("/activity?status=succeeded").text
    assert op in authed.get("/activity?status=pending&limit=5").text
    assert authed.get("/activity?status=bogus&limit=abc").status_code == 200


def test_jobs_page_escapes_and_handles_empty(authed, app):
    assert "No jobs" in authed.get("/jobs").text
    with app.db.tx() as c:
        c.execute("INSERT INTO jobs(id, type, client_id, account, status, spec_json, created_at, "
                  "updated_at) VALUES ('j1','reminder','hermes','t','active',?,'now','now')",
                  (f'{{"note":"{EVIL}"}}'.replace('"<', "'<"),))
    page = authed.get("/jobs")
    assert "j1" in page.text and EVIL not in page.text


def test_storage_page_reports_mode_and_encryption(authed, app):
    page = authed.get("/storage")
    assert page.status_code == 200
    assert "Storage mode" in page.text and "live" in page.text
    assert "Encrypted at rest" in page.text and "False" in page.text
    assert "Bridge keeps its own cache" in page.text
