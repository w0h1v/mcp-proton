"""send / reply / forward / reconcile against the real adapters (pymap + aiosmtpd)."""

from __future__ import annotations

import email
import email.policy

import pytest

from mcp_proton.domain.errors import ErrorCode, MailError
from mcp_proton.domain.models import (
    Address,
    ArtifactAttachment,
    LocalAttachment,
    MessageHandle,
    OperationStatus,
    OutgoingMessage,
)
from mcp_proton.policy.model import Constraints, Preset
from mcp_proton.services import attachments as att
from mcp_proton.services import sending

from .conftest import ACCOUNT, ADDRESS, make_raw


@pytest.fixture(autouse=True)
def _fast_sent_lookup(monkeypatch):
    monkeypatch.setattr(sending, "SENT_LOOKUP_ATTEMPTS", 1)
    monkeypatch.setattr(sending, "SENT_LOOKUP_DELAY", 0.0)


def out(**kw) -> OutgoingMessage:
    kw.setdefault("to", [Address(email="bob@example.com")])
    kw.setdefault("subject", "Hi")
    kw.setdefault("text", "hello bob")
    return OutgoingMessage(account=ACCOUNT, **kw)


def delivered(smtp_server, i=-1):
    mail_from, rcpts, data = smtp_server.messages[i]
    return mail_from, rcpts, email.message_from_bytes(data, policy=email.policy.default)


def constraints(tmp_path, **kw) -> Constraints:
    return Constraints(attachment_ingest_dirs=[str(tmp_path / "ingest")],
                       export_dirs=[str(tmp_path / "export")],
                       import_dirs=[str(tmp_path / "import")], **kw)


def test_send_success(app, caller, smtp_server):
    o = sending.send(app, caller, out())
    assert o.status is OperationStatus.SUCCEEDED
    assert o.result["status"] == "accepted"
    assert o.result["message_id"].startswith("<")
    assert [r["accepted"] for r in o.result["recipients"]] == [True]
    mail_from, rcpts, msg = delivered(smtp_server)
    assert mail_from == ADDRESS and rcpts == ["bob@example.com"]
    assert msg["Subject"] == "Hi" and msg["Message-ID"] == o.result["message_id"]
    assert msg.get_body().get_content().strip() == "hello bob"
    assert app.journal.get(o.operation_id).message_id == o.result["message_id"]


def test_send_does_not_append_to_sent(app, caller, smtp_server):
    sending.send(app, caller, out())
    store = app.store(ACCOUNT)
    _uv, uids = store.list_uids("Sent")
    assert uids == []  # Bridge files the copy; we only look for it


def test_sent_copy_reported_when_bridge_filed_it(app, caller, smtp_server, monkeypatch):
    store = app.store(ACCOUNT)
    real_send = app.transport(ACCOUNT).send

    def send_and_file(envelope_from, recipients, raw):
        res = real_send(envelope_from, recipients, raw)
        store.append("Sent", raw, flags=["\\Seen"])  # what Bridge does
        return res

    monkeypatch.setattr(app.transport(ACCOUNT), "send", send_and_file)
    o = sending.send(app, caller, out())
    assert o.result["sent_copy"]
    h = MessageHandle.parse(o.result["sent_copy"])
    assert h.mailbox == "Sent"


def test_bcc_in_envelope_not_in_headers(app, caller, smtp_server):
    o = sending.send(app, caller, out(cc=[Address(email="carol@example.com")],
                                       bcc=[Address(email="dave@example.com")]))
    assert o.status is OperationStatus.SUCCEEDED
    _f, rcpts, msg = delivered(smtp_server)
    assert set(rcpts) == {"bob@example.com", "carol@example.com", "dave@example.com"}
    assert msg["Bcc"] is None and "dave@example.com" not in smtp_server.messages[-1][2].decode()


def test_partial_recipient_rejection(app, caller, smtp_server):
    smtp_server.reject.add("bad@example.com")
    o = sending.send(app, caller, out(to=[Address(email="bob@example.com"),
                                          Address(email="bad@example.com")]))
    assert o.status is OperationStatus.PARTIALLY_SUCCEEDED
    assert o.result["status"] == "partially_accepted"
    by = {i.target: i for i in o.items}
    assert by["bob@example.com"].status == "succeeded"
    assert by["bad@example.com"].status == "failed" and "550" in by["bad@example.com"].detail
    assert smtp_server.messages[-1][1] == ["bob@example.com"]


def test_all_recipients_rejected_fails(app, caller, smtp_server):
    smtp_server.reject.add("bob@example.com")
    o = sending.send(app, caller, out())
    assert o.status is OperationStatus.FAILED
    assert o.result["status"] == "rejected"
    assert smtp_server.messages == []


def test_unknown_sender_identity_rejected(app, caller, smtp_server):
    with pytest.raises(MailError) as e:
        sending.send(app, caller, out(**{"from": Address(email="mallory@evil.test")}))
    assert e.value.code is ErrorCode.INVALID_REQUEST
    assert smtp_server.messages == []


def test_configured_identity_allowed_case_insensitive(app, caller, smtp_server):
    o = sending.send(app, caller, out(**{"from": Address(email="ALIAS@x.test")}))
    assert o.status is OperationStatus.SUCCEEDED
    assert smtp_server.messages[-1][0] == "ALIAS@x.test"


def test_recipients_required(app, caller):
    with pytest.raises(MailError) as e:
        sending.send(app, caller, out(to=[]))
    assert e.value.code is ErrorCode.INVALID_REQUEST


def test_header_injection_rejected(app, caller, smtp_server):
    with pytest.raises(MailError) as e:
        sending.send(app, caller, out(subject="x\r\nBcc: evil@example.com"))
    assert e.value.code is ErrorCode.INVALID_REQUEST
    assert smtp_server.messages == []


def test_recipient_constraint_blocks(make_app, caller, smtp_server, tmp_path):
    app = make_app(constraints=constraints(tmp_path, allowed_recipients=["@ok.test"]))
    with pytest.raises(MailError) as e:
        sending.send(app, caller, out())
    assert e.value.code is ErrorCode.CONSTRAINT_VIOLATION
    o = sending.send(app, caller, out(to=[Address(email="x@ok.test")]))
    assert o.status is OperationStatus.SUCCEEDED


def test_drop_after_data_is_delivery_unknown_and_never_resent(app, caller, owner, smtp_server):
    smtp_server.drop_after_data = True
    o = sending.send(app, caller, out(), idempotency_key="k1")
    assert o.status is OperationStatus.DELIVERY_UNKNOWN
    assert o.error["code"] == "delivery_unknown"
    assert len(smtp_server.messages) == 1
    mid = o.error["details"]["message_id"]
    # resume / status never re-execute a terminal operation
    assert app.resume(caller, o.operation_id).status is OperationStatus.DELIVERY_UNKNOWN
    # the same request again returns the same operation
    again = sending.send(app, caller, out(), idempotency_key="k1")
    assert again.operation_id == o.operation_id
    assert len(smtp_server.messages) == 1
    # reconcile is read-only evidence
    ev = sending.reconcile(app, caller, o.operation_id)
    assert ev["found_in_sent"] is False and ev["resent"] is False and ev["message_id"] == mid
    app.store(ACCOUNT).append("Sent", smtp_server.messages[0][2])
    ev = sending.reconcile(app, caller, o.operation_id)
    assert ev["found_in_sent"] is True and ev["sent_copy"]
    assert len(smtp_server.messages) == 1


def test_idempotent_send_returns_same_op_and_message_id(app, caller, smtp_server):
    a = sending.send(app, caller, out(), idempotency_key="same")
    b = sending.send(app, caller, out(), idempotency_key="same")
    assert a.operation_id == b.operation_id and len(smtp_server.messages) == 1
    with pytest.raises(MailError) as e:
        sending.send(app, caller, out(subject="different"), idempotency_key="same")
    assert e.value.code is ErrorCode.CONFLICT


def test_assistant_preset_pending_then_approved_sends_once(make_app, caller, owner, smtp_server,
                                                           tmp_path):
    app = make_app(Preset.ASSISTANT, constraints=constraints(tmp_path))
    o = sending.send(app, caller, out())
    assert o.status is OperationStatus.PENDING and smtp_server.messages == []
    done = app.approve(owner, o.operation_id, True, execute=True)
    assert done.status is OperationStatus.SUCCEEDED and len(smtp_server.messages) == 1
    again = app.resume(owner, o.operation_id)
    assert again.status is OperationStatus.SUCCEEDED and len(smtp_server.messages) == 1


def test_denied_approval_sends_nothing(make_app, caller, owner, smtp_server, tmp_path):
    app = make_app(Preset.ASSISTANT, constraints=constraints(tmp_path))
    o = sending.send(app, caller, out())
    r = app.approve(owner, o.operation_id, False)
    assert r.status is OperationStatus.DENIED and smtp_server.messages == []


def test_reader_cannot_send(make_app, caller, tmp_path):
    app = make_app(Preset.READER, constraints=constraints(tmp_path))
    with pytest.raises(MailError) as e:
        sending.send(app, caller, out())
    assert e.value.code is ErrorCode.POLICY_DENIED


def test_attachment_changed_after_approval_conflicts(make_app, caller, owner, smtp_server,
                                                      tmp_path):
    app = make_app(Preset.ASSISTANT, constraints=constraints(tmp_path))
    f = tmp_path / "ingest" / "report.txt"
    f.write_text("v1")
    o = sending.send(app, caller, out(attachments=[LocalAttachment(path=str(f))]))
    assert o.status is OperationStatus.PENDING
    assert o.summary.startswith("Send to 1 recipient")
    f.write_text("v2 swapped")
    done = app.approve(owner, o.operation_id, True, execute=True)
    assert done.status is OperationStatus.FAILED
    assert done.error["code"] == "conflict"
    assert smtp_server.messages == []


def test_local_attachment_outside_ingest_dir_rejected(app, caller, tmp_path, smtp_server):
    f = tmp_path / "secret.txt"
    f.write_text("nope")
    with pytest.raises(MailError) as e:
        sending.send(app, caller, out(attachments=[LocalAttachment(path=str(f))]))
    assert e.value.code is ErrorCode.PATH_REJECTED
    assert smtp_server.messages == []


def test_local_attachment_sent(app, caller, tmp_path, smtp_server):
    f = tmp_path / "ingest" / "report.txt"
    f.write_text("quarterly numbers")
    o = sending.send(app, caller, out(attachments=[LocalAttachment(path=str(f))]))
    assert o.status is OperationStatus.SUCCEEDED
    _f, _r, msg = delivered(smtp_server)
    parts = list(msg.iter_attachments())
    assert [p.get_filename() for p in parts] == ["report.txt"]
    assert parts[0].get_content().strip() == "quarterly numbers"


def test_attachment_size_limit_enforced(make_app, caller, tmp_path):
    app = make_app(constraints=constraints(tmp_path, max_attachment_bytes=10))
    f = tmp_path / "ingest" / "big.bin"
    f.write_bytes(b"x" * 11)
    with pytest.raises(MailError) as e:
        sending.send(app, caller, out(attachments=[LocalAttachment(path=str(f))]))
    assert e.value.code is ErrorCode.TOO_LARGE


def test_artifact_used_as_outgoing_attachment(app, caller, seed, smtp_server):
    import email.message

    m = email.message.EmailMessage()
    m["From"], m["To"], m["Subject"] = "a@example.com", ADDRESS, "with file"
    m["Message-ID"] = "<art1@example.com>"
    m.set_content("see attached")
    m.add_attachment(b"%PDF-fake", maintype="application", subtype="pdf", filename="inv.pdf")
    r = app.store(ACCOUNT).append("INBOX", m.as_bytes())
    handle = MessageHandle(account=ACCOUNT, mailbox="INBOX", uidvalidity=r.uidvalidity,
                           uid=r.uid).token()
    art = att.fetch_to_artifact(app, caller, handle, "2")
    o = sending.send(app, caller, out(attachments=[ArtifactAttachment(
        artifact_id=art["artifact_id"])]))
    assert o.status is OperationStatus.SUCCEEDED
    _f, _r, msg = delivered(smtp_server)
    part = next(msg.iter_attachments())
    assert part.get_filename() == "inv.pdf" and part.get_content() == b"%PDF-fake"


def test_reply_sets_threading_and_subject(app, caller, seed, smtp_server):
    [h] = seed(subject="Question", frm="alice@example.com", message_id="<q1@example.com>",
               body="what time?")
    o = sending.reply(app, caller, h, text="noon")
    assert o.status is OperationStatus.SUCCEEDED
    _f, rcpts, msg = delivered(smtp_server)
    assert rcpts == ["alice@example.com"]
    assert msg["Subject"] == "Re: Question"
    assert msg["In-Reply-To"] == "<q1@example.com>"
    assert "<q1@example.com>" in msg["References"]
    body = msg.get_body().get_content()
    assert body.startswith("noon") and "> what time?" in body


def test_reply_without_quote(app, caller, seed, smtp_server):
    [h] = seed(subject="Re: Question", body="quoted?")
    sending.reply(app, caller, h, text="no quote", quote=False)
    _f, _r, msg = delivered(smtp_server)
    assert msg["Subject"] == "Re: Question"  # no double prefix
    assert ">" not in msg.get_body().get_content()


def test_reply_all_excludes_self(app, caller, seed, smtp_server):
    [h] = seed(subject="Team", frm="alice@example.com", to=f"{ADDRESS}, bob@example.com",
               extra_headers="Cc: carol@example.com, Alias@x.test\r\n")
    o = sending.reply(app, caller, h, text="all", reply_all=True)
    assert o.status is OperationStatus.SUCCEEDED
    _f, rcpts, msg = delivered(smtp_server)
    assert set(rcpts) == {"alice@example.com", "bob@example.com", "carol@example.com"}
    assert ADDRESS not in rcpts and "alias@x.test" not in {r.lower() for r in rcpts}


def test_reply_answers_from_the_addressed_identity(app, caller, seed, smtp_server):
    [h] = seed(subject="To alias", frm="alice@example.com", to="alias@x.test")
    sending.reply(app, caller, h, text="hi")
    assert smtp_server.messages[-1][0].lower() == "alias@x.test"


def test_reply_needs_body(app, caller, seed):
    [h] = seed()
    with pytest.raises(MailError) as e:
        sending.reply(app, caller, h)
    assert e.value.code is ErrorCode.INVALID_REQUEST


def test_forward_inline(app, caller, seed, smtp_server):
    [h] = seed(subject="News", body="original body")
    o = sending.forward(app, caller, h, to=["carol@example.com"], text="fyi")
    assert o.status is OperationStatus.SUCCEEDED
    _f, rcpts, msg = delivered(smtp_server)
    assert rcpts == ["carol@example.com"] and msg["Subject"] == "Fwd: News"
    body = msg.get_body().get_content()
    assert "fyi" in body and "Forwarded message" in body and "original body" in body


def test_forward_as_attachment_contains_rfc822(app, caller, seed, smtp_server):
    [h] = seed(subject="News", body="original body")
    o = sending.forward(app, caller, h, to=[Address(email="carol@example.com")],
                        bcc=["dave@example.com"], text="see attached", as_attachment=True)
    assert o.status is OperationStatus.SUCCEEDED
    _f, rcpts, msg = delivered(smtp_server)
    assert set(rcpts) == {"carol@example.com", "dave@example.com"}
    inner = [p for p in msg.walk() if p.get_content_type() == "message/rfc822"]
    assert len(inner) == 1
    assert inner[0].get_payload()[0]["Subject"] == "News"


def test_forward_original_changed_conflicts(make_app, caller, owner, smtp_server, tmp_path):
    app = make_app(Preset.ASSISTANT, constraints=constraints(tmp_path))
    r = app.store(ACCOUNT).append("INBOX", make_raw(subject="orig"))
    h = MessageHandle(account=ACCOUNT, mailbox="INBOX", uidvalidity=r.uidvalidity,
                      uid=r.uid).token()
    o = sending.forward(app, caller, h, to=["c@example.com"], as_attachment=True)
    assert o.status is OperationStatus.PENDING
    # the original disappears before approval
    app.store(ACCOUNT).store_flags("INBOX", r.uidvalidity, [r.uid], add=["\\Deleted"])
    app.store(ACCOUNT).expunge_uids("INBOX", r.uidvalidity, [r.uid])
    done = app.approve(owner, o.operation_id, True, execute=True)
    assert done.status is OperationStatus.FAILED and done.error["code"] == "conflict"
    assert smtp_server.messages == []


def test_reconcile_rejects_non_send_operation(app, caller):
    from mcp_proton.services import drafts

    created = drafts.create_draft(app, caller, out())
    with pytest.raises(MailError) as e:
        sending.reconcile(app, caller, created.operation_id)
    assert e.value.code is ErrorCode.INVALID_REQUEST
