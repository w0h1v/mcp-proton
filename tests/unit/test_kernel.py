import threading

import pytest

from mcp_proton.config import AccountConfig, ServiceConfig
from mcp_proton.domain.errors import ErrorCode, MailError
from mcp_proton.domain.families import OperationFamily as F
from mcp_proton.domain.models import OperationStatus
from mcp_proton.domain.requests import CallerContext, OperationRequest
from mcp_proton.policy.model import ClientConfig, PolicyConfig, Preset
from mcp_proton.services.core import ExecResult, MailApp, executor
from mcp_proton.storage.db import Database

CALLS: list[dict] = []


@executor("test.send", F.SEND)
def _send(app, rec):
    CALLS.append(rec.request.payload)
    return ExecResult(result={"ok": True})


@executor("test.fail", F.ORGANIZE)
def _fail(app, rec):
    raise MailError(ErrorCode.STALE_HANDLE, "uidvalidity changed")


def make_app(preset=Preset.ASSISTANT, **policy_kw):
    cfg = ServiceConfig(accounts=[AccountConfig(name="me", address="me@x.test", username="me",
                                                secret_ref="env:X")])
    return MailApp(cfg, PolicyConfig(preset=preset, **policy_kw), Database(":memory:"),
                   store_factory=lambda a: None, transport_factory=lambda a: None)


HERMES = CallerContext(client_id="hermes")
OWNER = CallerContext.owner()


def send_req(body="hello", **kw):
    return OperationRequest(kind="test.send", family=F.SEND, account="me",
                            recipients=["a@x.test"], payload={"body": body}, **kw)


@pytest.fixture(autouse=True)
def _clear():
    CALLS.clear()


def test_allow_executes_and_journals():
    app = make_app(Preset.AUTONOMOUS)
    out = app.run(HERMES, send_req())
    assert out.status is OperationStatus.SUCCEEDED and CALLS == [{"body": "hello"}]
    assert app.status(HERMES, out.operation_id).status is OperationStatus.SUCCEEDED


def test_deny_raises_and_does_not_execute():
    app = make_app(Preset.READER)
    with pytest.raises(MailError) as e:
        app.run(HERMES, send_req())
    assert e.value.code is ErrorCode.POLICY_DENIED and not CALLS


def test_ask_flow_approve_then_resume_once():
    app = make_app()
    out = app.run(HERMES, send_req())
    assert out.status is OperationStatus.PENDING
    assert out.expires_at is not None and not CALLS
    # client resume before approval does nothing
    assert app.resume(HERMES, out.operation_id).status is OperationStatus.PENDING
    # client cannot approve itself
    with pytest.raises(MailError):
        app.approve(HERMES, out.operation_id)
    app.approve(OWNER, out.operation_id)
    r1 = app.resume(HERMES, out.operation_id)
    r2 = app.resume(HERMES, out.operation_id)
    assert r1.status is OperationStatus.SUCCEEDED and r2.status is OperationStatus.SUCCEEDED
    assert len(CALLS) == 1


def test_concurrent_resume_executes_once():
    app = make_app()
    out = app.run(HERMES, send_req())
    app.approve(OWNER, out.operation_id)
    threads = [threading.Thread(target=app.resume, args=(HERMES, out.operation_id))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(CALLS) == 1


def test_other_client_cannot_see_or_resume():
    app = make_app()
    out = app.run(HERMES, send_req())
    with pytest.raises(MailError) as e:
        app.status(CallerContext(client_id="other"), out.operation_id)
    assert e.value.code is ErrorCode.NOT_FOUND


def test_revocation_after_approval_blocks_resume():
    app = make_app()
    out = app.run(HERMES, send_req())
    app.approve(OWNER, out.operation_id)
    app.set_policy(PolicyConfig(preset=Preset.ASSISTANT,
                                clients=[ClientConfig(client_id="hermes", revoked=True)]))
    with pytest.raises(MailError) as e:
        app.resume(HERMES, out.operation_id)
    assert e.value.code is ErrorCode.CLIENT_REVOKED and not CALLS
    assert app.status(OWNER, out.operation_id).status is OperationStatus.CANCELLED


def test_denied_decision_is_terminal():
    app = make_app()
    out = app.run(HERMES, send_req())
    app.approve(OWNER, out.operation_id, approve=False)
    assert app.resume(HERMES, out.operation_id).status is OperationStatus.DENIED and not CALLS


def test_expired_approval(monkeypatch):
    app = make_app()
    out = app.run(HERMES, send_req())
    app.db.query("UPDATE operations SET expires_at='2000-01-01T00:00:00+00:00'")
    assert app.status(HERMES, out.operation_id).status is OperationStatus.EXPIRED
    with pytest.raises(MailError):
        app.approve(OWNER, out.operation_id)


def test_reviewer_channel_approves_inline():
    app = make_app()
    out = app.run(HERMES, send_req(), reviewer=lambda rec: True)
    assert out.status is OperationStatus.SUCCEEDED and len(CALLS) == 1


def test_reviewer_none_leaves_pending():
    app = make_app()
    out = app.run(HERMES, send_req(), reviewer=lambda rec: None)
    assert out.status is OperationStatus.PENDING


def test_idempotency_key_dedupes_and_detects_changes():
    app = make_app(Preset.AUTONOMOUS)
    a = app.run(HERMES, send_req(idempotency_key="k1"))
    b = app.run(HERMES, send_req(idempotency_key="k1"))
    assert a.operation_id == b.operation_id and len(CALLS) == 1
    with pytest.raises(MailError) as e:
        app.run(HERMES, send_req(body="changed", idempotency_key="k1"))
    assert e.value.code is ErrorCode.CONFLICT


def test_executor_error_journaled_as_failed():
    app = make_app()
    out = app.run(HERMES, OperationRequest(kind="test.fail", family=F.ORGANIZE, account="me"))
    assert out.status is OperationStatus.FAILED and out.error["code"] == "stale_handle"


def test_unregistered_kind_rejected():
    app = make_app(Preset.AUTONOMOUS)
    with pytest.raises(MailError) as e:
        app.run(HERMES, OperationRequest(kind="nope", family=F.READ, account="me"))
    assert e.value.code is ErrorCode.UNSUPPORTED


def test_send_limit():
    from mcp_proton.policy.model import Constraints
    app = make_app(Preset.AUTONOMOUS, constraints=Constraints(max_sends_per_day=1))
    app.run(HERMES, send_req())
    with pytest.raises(MailError) as e:
        app.run(HERMES, send_req(body="2"))
    assert e.value.code is ErrorCode.LIMIT_EXCEEDED
