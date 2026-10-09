"""Webhooks: signed metadata-only deliveries, retries, catch-up, URL rules, secret shown once."""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from tests.jobs.conftest import ACCOUNT
from tests.services.conftest import make_raw

from mcp_proton.domain.errors import ErrorCode, MailError
from mcp_proton.domain.models import OperationStatus
from mcp_proton.jobs import reminders, webhooks
from mcp_proton.jobs.webhooks import Dispatcher, sign, validate_url
from mcp_proton.services import events

SUBJECT = "Confidential merger terms"


class Sink:
    """A local HTTP receiver; ``fail`` is how many of the next requests answer 500."""

    def __init__(self) -> None:
        self.requests: list[tuple[dict[str, str], bytes]] = []
        self.fail = 0
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                sink.requests.append(({k.lower(): v for k, v in self.headers.items()}, body))
                if sink.fail > 0:
                    sink.fail -= 1
                    self.send_response(500)
                else:
                    self.send_response(204)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/hook"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def sink():
    s = Sink()
    yield s
    s.close()


def new_mail_event(app, subject=SUBJECT):
    rec = events.Reconciler(app)
    rec.reconcile(ACCOUNT, "INBOX")
    app.store(ACCOUNT).append("INBOX", make_raw(subject=subject))
    rec.reconcile(ACCOUNT, "INBOX")


def register(app, caller, sink, **kw):
    out = webhooks.create_with_secret(app, caller, ACCOUNT, sink.url, **kw)
    assert out.status is OperationStatus.SUCCEEDED, out
    return out.result["webhook_id"], out.result["secret"]


def test_delivery_is_signed_and_metadata_only(app, caller, sink, clock, make_scheduler):
    wh_id, secret = register(app, caller, sink)
    new_mail_event(app)
    rep = make_scheduler(app).tick(clock.after(seconds=1))
    assert rep.webhook_deliveries == 1
    [(headers, body)] = sink.requests

    ts = int(headers[webhooks.TIMESTAMP_HEADER.lower()])
    assert headers[webhooks.SIGNATURE_HEADER.lower()] == sign(secret, ts, body)
    assert headers[webhooks.SIGNATURE_HEADER.lower()] != sign("wrong-secret", ts, body)
    assert headers[webhooks.EVENT_HEADER.lower()] == "message_added"

    payload = json.loads(body)
    assert payload["type"] == "message_added" and payload["hint"] is True
    assert payload["data"]["handle"].startswith("h1.") and payload["mailbox"] == "INBOX"
    assert SUBJECT.encode() not in body and b"alice@example.com" not in body
    assert "subject" not in body.decode().lower()
    [row] = webhooks.list_webhooks(app, caller)
    assert row["webhook_id"] == wh_id and "secret" not in row and row["cursor"] > 0


def test_retry_with_backoff_then_success(app, caller, sink, clock):
    register(app, caller, sink)
    new_mail_event(app)
    sleeps: list[float] = []
    disp = Dispatcher(app, sleep_fn=sleeps.append, max_attempts=3, base_delay=1.0)
    sink.fail = 2
    assert disp.deliver_all(clock.after(seconds=1)) == 1
    assert len(sink.requests) == 3 and sleeps == [1.0, 2.0]
    attempts = [json.loads(b)["attempt"] for _h, b in sink.requests]
    assert attempts == [1, 2, 3]


def test_outage_then_catch_up_without_loss(app, caller, sink, clock):
    register(app, caller, sink)
    disp = Dispatcher(app, sleep_fn=lambda s: None, max_attempts=2)
    rec = events.Reconciler(app)
    rec.reconcile(ACCOUNT, "INBOX")
    for i in range(3):
        app.store(ACCOUNT).append("INBOX", make_raw(subject=f"m{i}"))
    rec.reconcile(ACCOUNT, "INBOX")

    now = clock.after(seconds=1)
    sink.fail = 10  # destination down
    assert disp.deliver_all(now) == 0
    [row] = webhooks.list_webhooks(app, caller)
    assert row["consecutive_failures"] == 1 and row["cursor"] == 0 and row["last_error"]

    sink.fail = 0
    sink.requests.clear()
    assert disp.deliver_all(now + timedelta(seconds=5)) == 0  # still backing off
    assert disp.deliver_all(now + timedelta(minutes=5)) == 3  # caught up, in order
    ids = [json.loads(b)["id"] for _h, b in sink.requests]
    assert ids == sorted(ids) and len(ids) == 3
    [row] = webhooks.list_webhooks(app, caller)
    assert row["consecutive_failures"] == 0 and row["last_error"] is None
    assert disp.deliver_all(now + timedelta(minutes=10)) == 0  # nothing re-sent


def test_event_type_filter_and_reminder_note_redacted(app, caller, sink, clock, seed):
    register(app, caller, sink, event_types=["reminder_due"])
    [h] = seed()
    due = clock.after(hours=1)
    reminders.remind(app, caller, h, due, note="private note text")
    new_mail_event(app)  # message_added is filtered out
    from mcp_proton.jobs import Scheduler

    sched = Scheduler(app, now_fn=clock, dispatcher=Dispatcher(app, sleep_fn=lambda s: None))
    sched.tick(due + timedelta(seconds=1))
    [(_h, body)] = sink.requests
    payload = json.loads(body)
    assert payload["type"] == "reminder_due"
    assert b"private note text" not in body and "note" not in payload["data"]


def test_secret_is_shown_once(app, caller, sink):
    out = webhooks.create(app, caller, ACCOUNT, sink.url)
    assert "withheld" in out.result["secret"]
    secret = webhooks.reveal_secret(app, caller, out.result["webhook_id"])
    assert secret.startswith("whsec_")
    with pytest.raises(MailError) as e:
        webhooks.reveal_secret(app, caller, out.result["webhook_id"])
    assert e.value.code is ErrorCode.CONFLICT


def test_url_rules():
    assert validate_url("https://hooks.example.com/x") == "https://hooks.example.com/x"
    assert validate_url("http://127.0.0.1:9/x") and validate_url("http://localhost/x")
    assert validate_url("http://[::1]:9/x")
    for bad in ("http://hooks.example.com/x", "ftp://example.com", "https://u:p@example.com/",
                "https:///nohost", "not a url", "http://10.0.0.5/x", "https://x.test/#frag"):
        with pytest.raises(MailError):
            validate_url(bad)


def test_unknown_event_type_rejected(app, caller, sink):
    with pytest.raises(MailError):
        webhooks.create(app, caller, ACCOUNT, sink.url, ["subject_changed"])


def test_scoping_and_delete(app, caller, owner, sink, clock):
    from mcp_proton.domain.requests import CallerContext

    wh_id, _ = register(app, caller, sink)
    other = CallerContext(client_id="other")
    assert webhooks.list_webhooks(app, other) == []
    assert len(webhooks.list_webhooks(app, owner)) == 1
    with pytest.raises(MailError):
        webhooks.delete(app, other, wh_id)
    out = webhooks.delete(app, caller, wh_id)
    assert out.status is OperationStatus.SUCCEEDED
    assert webhooks.list_webhooks(app, caller) == []
    new_mail_event(app)
    assert Dispatcher(app, sleep_fn=lambda s: None).deliver_all(clock.after(seconds=1)) == 0
    assert sink.requests == []


def test_assistant_registration_is_pending(make_app, caller, owner, sink):
    from mcp_proton.policy.model import Preset

    app = make_app(Preset.ASSISTANT)
    out = webhooks.create_with_secret(app, caller, ACCOUNT, sink.url)
    assert out.status is OperationStatus.PENDING
    assert webhooks.list_webhooks(app, caller) == []
    app.approve(owner, out.operation_id, True, execute=True)
    [row] = webhooks.list_webhooks(app, caller)
    assert webhooks.reveal_secret(app, caller, row["webhook_id"])  # shown once, later


def test_revoked_client_stops_receiving_but_keeps_position(make_app, owner, sink, clock):
    from mcp_proton.domain.requests import CallerContext
    from mcp_proton.policy.model import ClientConfig, PolicyConfig, Preset

    app = make_app(Preset.AUTONOMOUS, clients=[ClientConfig(client_id="hermes")])
    caller = CallerContext(client_id="hermes")
    register(app, caller, sink)
    new_mail_event(app)
    app.set_policy(PolicyConfig(preset=Preset.AUTONOMOUS,
                                clients=[ClientConfig(client_id="hermes", revoked=True)]))
    disp = Dispatcher(app, sleep_fn=lambda s: None)
    assert disp.deliver_all(clock.after(seconds=1)) == 0 and sink.requests == []
    app.set_policy(PolicyConfig(preset=Preset.AUTONOMOUS,
                                clients=[ClientConfig(client_id="hermes")]))
    assert disp.deliver_all(clock.after(seconds=2)) == 1  # position was held
