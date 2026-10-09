"""SmtpTransport against the aiosmtpd fixture (plaintext loopback) and stubs."""

from __future__ import annotations

import smtplib
import socket
import threading

import pytest

from mcp_proton.bridge import smtp as smtp_mod
from mcp_proton.bridge.ports import SmtpDeliveryUnknown, SmtpSendError
from mcp_proton.bridge.smtp import SmtpTransport, open_transport
from mcp_proton.config import AccountConfig, Security
from mcp_proton.domain.errors import ErrorCode, MailError

RAW = b"From: a@example.test\r\nTo: b@example.test\r\nSubject: t\r\n\r\nbody\r\n"


def account(host: str, port: int, **kw) -> AccountConfig:
    base = {
        "name": "acct", "address": "a@example.test", "username": "a@example.test",
        "secret_ref": "env:MCP_PROTON_TEST_SECRET", "smtp_host": host, "smtp_port": port,
        "smtp_security": Security.NONE, "connect_timeout": 5.0,
    }
    base.update(kw)
    return AccountConfig(**base)


@pytest.fixture
def transport(smtp_server):
    return SmtpTransport(account(smtp_server.host, smtp_server.port))


def test_success(smtp_server, transport):
    res = transport.send("a@example.test", ["b@example.test", "c@example.test"], RAW)
    assert [(r.email, r.accepted, r.code) for r in res] == [
        ("b@example.test", True, 250), ("c@example.test", True, 250)]
    [(mail_from, rcpts, content)] = smtp_server.messages
    assert mail_from == "a@example.test" and rcpts == ["b@example.test", "c@example.test"]
    assert b"Subject: t" in content


def test_bare_lf_normalized_to_crlf(smtp_server, transport):
    transport.send("a@example.test", ["b@example.test"], b"Subject: t\n\nline\n")
    assert b"\n" not in smtp_server.messages[0][2].replace(b"\r\n", b"")


def test_partial_rejection(smtp_server, transport):
    smtp_server.reject.add("bad@example.test")
    res = transport.send("a@example.test", ["good@example.test", "bad@example.test"], RAW)
    by = {r.email: r for r in res}
    assert by["good@example.test"].accepted is True
    assert by["bad@example.test"].accepted is False and by["bad@example.test"].code == 550
    assert smtp_server.messages[0][1] == ["good@example.test"]


def test_all_rejected_sends_no_data(smtp_server, transport):
    smtp_server.reject.update({"x@example.test", "y@example.test"})
    res = transport.send("a@example.test", ["x@example.test", "y@example.test"], RAW)
    assert [r.accepted for r in res] == [False, False]
    assert smtp_server.messages == []


class DroppingServer:
    """Tiny SMTP server that reads the DATA payload, then closes without a final reply.

    Used instead of the ``smtp_server`` fixture's ``drop_after_data`` flag, which
    cannot work as written (``aiosmtpd`` sessions have no ``transport`` attribute, so
    the handler errors and the server answers 500 rather than dropping the link).
    """

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received_data = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        conn, _ = self.sock.accept()
        with conn, conn.makefile("rwb") as f:
            def reply(line: bytes) -> None:
                f.write(line + b"\r\n")
                f.flush()

            reply(b"220 fake ESMTP")
            in_data = False
            for line in f:
                if in_data:
                    if line == b".\r\n":
                        self.received_data = True
                        return  # drop: no final reply
                    continue
                cmd = line.strip().upper()
                if cmd.startswith(b"EHLO"):
                    reply(b"250 fake")
                elif cmd == b"DATA":
                    in_data = True
                    reply(b"354 go")
                else:
                    reply(b"250 OK")
        self.sock.close()


def test_drop_after_data_is_delivery_unknown():
    srv = DroppingServer()
    t = SmtpTransport(account("127.0.0.1", srv.port))
    with pytest.raises(SmtpDeliveryUnknown) as e:
        t.send("a@example.test", ["b@example.test", "c@example.test"], RAW)
    srv.thread.join(5)
    assert srv.received_data  # the server did get the message: the ambiguity is real
    assert [(r.email, r.accepted) for r in e.value.recipients] == [
        ("b@example.test", True), ("c@example.test", True)]


def test_connection_refused_is_send_error():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again on exit: nothing listens
    with pytest.raises(SmtpSendError):
        SmtpTransport(account("127.0.0.1", port)).send("a@example.test", ["b@example.test"], RAW)


def test_check(smtp_server, transport):
    transport.check()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(MailError) as e:
        SmtpTransport(account("127.0.0.1", port)).check()
    assert e.value.code is ErrorCode.BRIDGE_UNAVAILABLE


def test_plaintext_refused_for_non_loopback():
    t = SmtpTransport(account("192.0.2.1", 25))
    with pytest.raises(MailError) as e:
        t.check()
    assert e.value.code is ErrorCode.TLS_ERROR
    with pytest.raises(MailError):  # TLS_ERROR is not retryable: not wrapped
        t.send("a@example.test", ["b@example.test"], RAW)


def test_open_transport_factory(smtp_server):
    t = open_transport(account(smtp_server.host, smtp_server.port))
    assert isinstance(t, SmtpTransport) and t.account == "acct"


# ---- stubbed smtplib: DATA-stage outcomes the fixture cannot produce deterministically


class FakeSMTP:
    """Minimal smtplib.SMTP stand-in; ``data_behaviour`` drives the DATA stage."""

    def __init__(self, data_behaviour):
        self.behaviour = data_behaviour
        self.closed = False
        self.data_calls = 0

    def has_extn(self, name):
        return False

    def mail(self, sender):
        return 250, b"ok"

    def rcpt(self, rcpt):
        return 250, b"ok"

    def data(self, raw):
        self.data_calls += 1
        return self.behaviour()

    def quit(self):
        self.closed = True

    def close(self):
        self.closed = True


def stubbed(monkeypatch, behaviour) -> tuple[SmtpTransport, FakeSMTP]:
    fake = FakeSMTP(behaviour)
    t = SmtpTransport(account("127.0.0.1", 1))
    monkeypatch.setattr(t, "_open", lambda: fake)
    return t, fake


def raising(exc):
    def f():
        raise exc

    return f


@pytest.mark.parametrize(
    "exc", [smtplib.SMTPServerDisconnected("gone"), TimeoutError("slow"), OSError("reset"),
            RuntimeError("weird")]
)
def test_failure_after_data_command_is_unknown(monkeypatch, exc):
    t, fake = stubbed(monkeypatch, raising(exc))
    with pytest.raises(SmtpDeliveryUnknown) as e:
        t.send("a@example.test", ["b@example.test"], RAW)
    assert e.value.recipients[0].accepted is True and fake.closed and fake.data_calls == 1


def test_data_refused_marks_all_not_accepted(monkeypatch):
    t, _ = stubbed(monkeypatch, raising(smtplib.SMTPDataError(451, b"try later")))
    [r] = t.send("a@example.test", ["b@example.test"], RAW)
    assert r.accepted is False and r.code == 451 and r.message == "try later"


@pytest.mark.parametrize("code,ok", [(250, True), (552, False), (451, False)])
def test_final_reply_code(monkeypatch, code, ok):
    t, _ = stubbed(monkeypatch, lambda: (code, b"reply"))
    [r] = t.send("a@example.test", ["b@example.test"], RAW)
    assert r.accepted is ok and (r.code == 250 if ok else r.code == code)


def test_mail_from_rejected_is_send_error(monkeypatch):
    t, fake = stubbed(monkeypatch, lambda: (250, b""))
    monkeypatch.setattr(fake, "mail", lambda s: (553, b"nope"))
    with pytest.raises(SmtpSendError):
        t.send("a@example.test", ["b@example.test"], RAW)
    assert fake.data_calls == 0


def test_disconnect_during_rcpt_is_send_error_not_unknown(monkeypatch):
    t, fake = stubbed(monkeypatch, lambda: (250, b""))
    monkeypatch.setattr(fake, "rcpt", raising(smtplib.SMTPServerDisconnected("gone")))
    with pytest.raises(SmtpSendError):
        t.send("a@example.test", ["b@example.test"], RAW)
    assert fake.data_calls == 0


def test_auth_required_without_auth_extension_over_tls(monkeypatch):
    class NoAuth(FakeSMTP):
        sock = None

        def ehlo(self):
            return 250, b""

        def starttls(self, context=None):
            return 220, b""

    monkeypatch.setattr(smtp_mod.smtplib, "SMTP", lambda *a, **k: NoAuth(lambda: (250, b"")))
    monkeypatch.setattr(smtp_mod.tls, "verify_peer", lambda *a: None)
    t = SmtpTransport(account("127.0.0.1", 1, smtp_security=Security.STARTTLS))
    # server without STARTTLS support is a TLS error, never a silent plaintext fallback
    with pytest.raises(MailError) as e:
        t.check()
    assert e.value.code is ErrorCode.TLS_ERROR
