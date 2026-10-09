"""Drafts: create / read / update / discard / send against pymap + aiosmtpd."""

from __future__ import annotations

import email
import email.policy

import pytest

from mcp_proton.domain.errors import ErrorCode, MailError
from mcp_proton.domain.families import OperationFamily
from mcp_proton.domain.models import (
    Address,
    LocalAttachment,
    MessageHandle,
    OperationStatus,
    OutgoingMessage,
)
from mcp_proton.policy.model import Action, Constraints, PolicyRule, Preset
from mcp_proton.services import drafts, sending

from .conftest import ACCOUNT, ADDRESS, make_raw


@pytest.fixture(autouse=True)
def _fast_sent_lookup(monkeypatch):
    monkeypatch.setattr(sending, "SENT_LOOKUP_ATTEMPTS", 1)
    monkeypatch.setattr(sending, "SENT_LOOKUP_DELAY", 0.0)


def out(**kw) -> OutgoingMessage:
    kw.setdefault("to", [Address(email="bob@example.com")])
    kw.setdefault("bcc", [Address(email="dave@example.com")])
    kw.setdefault("subject", "Draft subject")
    kw.setdefault("text", "draft body")
    return OutgoingMessage(account=ACCOUNT, **kw)


def constraints(tmp_path) -> Constraints:
    return Constraints(attachment_ingest_dirs=[str(tmp_path / "ingest")],
                       export_dirs=[str(tmp_path / "export")],
                       import_dirs=[str(tmp_path / "import")])


def drafts_uids(app) -> list[int]:
    return app.store(ACCOUNT).list_uids("Drafts")[1]


def create(app, caller, **kw) -> str:
    o = drafts.create_draft(app, caller, out(**kw))
    assert o.status is OperationStatus.SUCCEEDED, o
    return o.result["handle"]


def test_create_and_read_draft(app, caller):
    h = create(app, caller)
    assert len(drafts_uids(app)) == 1
    d = drafts.get_draft(app, caller, h)
    assert d["draft"]["subject"] == "Draft subject"
    assert d["draft"]["text"].strip() == "draft body"
    assert [a["email"] for a in d["draft"]["to"]] == ["bob@example.com"]
    assert [a["email"] for a in d["draft"]["bcc"]] == ["dave@example.com"]  # Bcc survives
    assert "\\Draft" in d["flags"] and d["attachments"] == []
    assert d["revision"] and d["message_id"]


def test_create_is_idempotent_and_never_duplicates(app, caller):
    first = drafts.create_draft(app, caller, out(), idempotency_key="k")
    again = drafts.create_draft(app, caller, out(), idempotency_key="k")
    assert first.operation_id == again.operation_id
    assert len(drafts_uids(app)) == 1


def test_retried_executor_finds_existing_draft(app, caller):
    o = drafts.create_draft(app, caller, out())
    rec = app.journal.get(o.operation_id)
    res = drafts._exec_create(app, rec)  # a resume after a crash between append and journal
    assert res.result["duplicate_avoided"] is True
    assert res.result["handle"] == o.result["handle"]
    assert len(drafts_uids(app)) == 1


def test_get_draft_rejects_non_draft(app, caller, seed):
    [h] = seed()
    with pytest.raises(MailError) as e:
        drafts.get_draft(app, caller, h)
    assert e.value.code is ErrorCode.INVALID_REQUEST


def test_draft_with_local_attachment_and_ingest_scope(app, caller, tmp_path):
    f = tmp_path / "ingest" / "a.txt"
    f.write_text("attached")
    h = create(app, caller, attachments=[LocalAttachment(path=str(f))])
    d = drafts.get_draft(app, caller, h)
    assert [a["filename"] for a in d["attachments"]] == ["a.txt"]
    outside = tmp_path / "other.txt"
    outside.write_text("x")
    with pytest.raises(MailError) as e:
        drafts.create_draft(app, caller, out(attachments=[LocalAttachment(path=str(outside))]))
    assert e.value.code is ErrorCode.PATH_REJECTED


def test_update_replaces_and_returns_new_handle(app, caller):
    h = create(app, caller)
    o = drafts.update_draft(app, caller, h, {"subject": "New subject", "text": "new body",
                                             "cc": ["carol@example.com"]})
    assert o.status is OperationStatus.SUCCEEDED
    new = o.result["handle"]
    assert new != h and o.result["old_handle"] == h and o.result["old_removed"] is True
    assert len(drafts_uids(app)) == 1  # old occurrence gone, exactly one draft
    d = drafts.get_draft(app, caller, new)
    assert d["draft"]["subject"] == "New subject" and d["draft"]["text"].strip() == "new body"
    assert [a["email"] for a in d["draft"]["cc"]] == ["carol@example.com"]
    assert [a["email"] for a in d["draft"]["bcc"]] == ["dave@example.com"]  # carried over
    assert o.items[0].target == h and o.items[0].new_handle == new
    with pytest.raises(MailError):  # the old handle is no longer addressable
        drafts.get_draft(app, caller, h)


def test_update_keeps_adds_and_removes_attachments(app, caller, tmp_path):
    a = tmp_path / "ingest" / "a.txt"
    b = tmp_path / "ingest" / "b.txt"
    a.write_text("A")
    b.write_text("B")
    h = create(app, caller, attachments=[LocalAttachment(path=str(a))])
    h2 = drafts.update_draft(app, caller, h, {"subject": "s2"}).result["handle"]
    assert [x["filename"] for x in drafts.get_draft(app, caller, h2)["attachments"]] == ["a.txt"]
    o = drafts.update_draft(app, caller, h2, {"add_attachments": [{"path": str(b)}],
                                              "remove_attachments": ["2"]})
    final = drafts.get_draft(app, caller, o.result["handle"])
    names = [x["filename"] for x in final["attachments"]]
    assert names == ["b.txt"]


def test_update_rejects_unknown_change_and_bad_part(app, caller):
    h = create(app, caller)
    with pytest.raises(MailError):
        drafts.update_draft(app, caller, h, {"flags": ["x"]})
    with pytest.raises(MailError):
        drafts.update_draft(app, caller, h, {"remove_attachments": ["9"]})


def test_update_conflict_when_draft_replaced_meanwhile(make_app, caller, owner, tmp_path):
    app = make_app(Preset.ASSISTANT, constraints=constraints(tmp_path), rules=[
        PolicyRule(family=OperationFamily.DRAFTS, action=Action.ASK)])
    store = app.store(ACCOUNT)
    r = store.append("Drafts", make_raw(subject="mine", to="bob@example.com"),
                     flags=["\\Draft"])
    h = MessageHandle(account=ACCOUNT, mailbox="Drafts", uidvalidity=r.uidvalidity,
                      uid=r.uid).token()
    o = drafts.update_draft(app, caller, h, {"subject": "edited by agent"})
    assert o.status is OperationStatus.PENDING
    # another client replaces the draft before the owner approves
    store.store_flags("Drafts", r.uidvalidity, [r.uid], add=["\\Deleted"])
    store.expunge_uids("Drafts", r.uidvalidity, [r.uid])
    store.append("Drafts", make_raw(subject="edited elsewhere"), flags=["\\Draft"])
    done = app.approve(owner, o.operation_id, True, execute=True)
    assert done.status is OperationStatus.FAILED and done.error["code"] == "conflict"
    assert len(drafts_uids(app)) == 1  # nothing appended


def test_update_cleanup_failure_is_partial_and_keeps_both_handles(app, caller, monkeypatch):
    h = create(app, caller)
    store = app.store(ACCOUNT)

    def boom(*a, **k):
        raise MailError(ErrorCode.BRIDGE_UNAVAILABLE, "expunge failed")

    monkeypatch.setattr(store, "expunge_uids", boom)
    o = drafts.update_draft(app, caller, h, {"subject": "v2"})
    assert o.status is OperationStatus.PARTIALLY_SUCCEEDED
    assert o.result["old_removed"] is False and o.result["old_handle"] == h
    assert o.result["handle"] != h and "cleanup_error" in o.result
    # new version verified and present; old one is not left flagged deleted
    flags = store.fetch_flags("Drafts", MessageHandle.parse(h).uidvalidity,
                              [MessageHandle.parse(h).uid])
    assert "\\Deleted" not in next(iter(flags.values()))
    assert len(drafts_uids(app)) == 2


def test_discard_removes_only_that_draft(app, caller):
    h1, h2 = create(app, caller, subject="one"), create(app, caller, subject="two")
    o = drafts.discard_draft(app, caller, h1)
    assert o.status is OperationStatus.SUCCEEDED and o.result["removed"] is True
    assert drafts_uids(app) == [MessageHandle.parse(h2).uid]
    again = drafts.discard_draft(app, caller, h1)  # already gone: idempotent
    assert again.status is OperationStatus.SUCCEEDED and again.items[0].status == "skipped"


def test_discard_refuses_non_draft(app, caller, seed):
    [h] = seed()
    with pytest.raises(MailError) as e:
        drafts.discard_draft(app, caller, h)
    assert e.value.code is ErrorCode.UNSUPPORTED_SEMANTICS
    assert len(app.store(ACCOUNT).list_uids("INBOX")[1]) == 1


def test_send_draft_then_cleanup(app, caller, smtp_server):
    h = create(app, caller)
    mid = drafts.get_draft(app, caller, h)["message_id"]
    o = drafts.send_draft(app, caller, h)
    assert o.status is OperationStatus.SUCCEEDED
    assert o.result["draft_removed"] is True and o.result["message_id"] == mid
    assert drafts_uids(app) == []
    mail_from, rcpts, data = smtp_server.messages[-1]
    assert set(rcpts) == {"bob@example.com", "dave@example.com"} and mail_from == ADDRESS
    msg = email.message_from_bytes(data, policy=email.policy.default)
    assert msg["Bcc"] is None and msg["Message-ID"] == mid and msg["Subject"] == "Draft subject"
    assert "dave@example.com" not in data.decode()


def test_send_draft_with_attachment_keeps_it(app, caller, smtp_server, tmp_path):
    f = tmp_path / "ingest" / "doc.txt"
    f.write_text("payload")
    h = create(app, caller, attachments=[LocalAttachment(path=str(f))])
    assert drafts.send_draft(app, caller, h).status is OperationStatus.SUCCEEDED
    msg = email.message_from_bytes(smtp_server.messages[-1][2], policy=email.policy.default)
    assert [p.get_filename() for p in msg.iter_attachments()] == ["doc.txt"]


def test_send_draft_cleanup_failure_reported(app, caller, smtp_server, monkeypatch):
    h = create(app, caller)

    def boom(*a, **k):
        raise MailError(ErrorCode.BRIDGE_UNAVAILABLE, "expunge failed")

    monkeypatch.setattr(app.store(ACCOUNT), "expunge_uids", boom)
    o = drafts.send_draft(app, caller, h)
    assert o.status is OperationStatus.SUCCEEDED  # the mail was accepted
    assert o.result["draft_removed"] is False and o.result["draft_cleanup_error"]
    assert len(smtp_server.messages) == 1


def test_send_draft_partial_keeps_draft(app, caller, smtp_server):
    smtp_server.reject.add("dave@example.com")
    h = create(app, caller)
    o = drafts.send_draft(app, caller, h)
    assert o.status is OperationStatus.PARTIALLY_SUCCEEDED
    assert o.result["draft_removed"] is False and len(drafts_uids(app)) == 1


def test_send_draft_delivery_unknown_keeps_draft_and_never_resends(app, caller, smtp_server):
    smtp_server.drop_after_data = True
    h = create(app, caller)
    o = drafts.send_draft(app, caller, h, idempotency_key="d1")
    assert o.status is OperationStatus.DELIVERY_UNKNOWN
    assert len(drafts_uids(app)) == 1
    assert app.resume(caller, o.operation_id).status is OperationStatus.DELIVERY_UNKNOWN
    assert drafts.send_draft(app, caller, h, idempotency_key="d1").operation_id == o.operation_id
    assert len(smtp_server.messages) == 1


def test_send_draft_changed_after_approval_conflicts(make_app, caller, owner, smtp_server,
                                                      tmp_path):
    app = make_app(Preset.ASSISTANT, constraints=constraints(tmp_path))
    h = create(app, caller)
    o = drafts.send_draft(app, caller, h)
    assert o.status is OperationStatus.PENDING
    drafts.update_draft(app, caller, h, {"text": "sneaky change"})  # drafts are Allow here
    done = app.approve(owner, o.operation_id, True, execute=True)
    assert done.status is OperationStatus.FAILED and done.error["code"] == "conflict"
    assert smtp_server.messages == []


def test_send_draft_requires_recipients_and_known_sender(app, caller):
    h = create(app, caller, to=[], bcc=[])
    with pytest.raises(MailError) as e:
        drafts.send_draft(app, caller, h)
    assert e.value.code is ErrorCode.INVALID_REQUEST
    store = app.store(ACCOUNT)
    r = store.append("Drafts", make_raw(frm="mallory@evil.test", to="bob@example.com"),
                     flags=["\\Draft"])
    foreign = MessageHandle(account=ACCOUNT, mailbox="Drafts", uidvalidity=r.uidvalidity,
                            uid=r.uid).token()
    with pytest.raises(MailError) as e2:
        drafts.send_draft(app, caller, foreign)
    assert e2.value.code is ErrorCode.INVALID_REQUEST


def test_prepare_outgoing_strips_bcc_including_folded_lines():
    raw = (b"From: a@x.test\r\nBcc: one@x.test,\r\n two@x.test\r\nSubject: s\r\n"
           b"Date: Mon, 01 Jan 2024 00:00:00 +0000\r\n\r\nBcc: not a header\r\n")
    prepared = drafts.prepare_outgoing(raw, "<new@x.test>")
    head, body = prepared.split(b"\r\n\r\n", 1)
    assert b"one@x.test" not in head and b"two@x.test" not in head
    assert b"Message-ID: <new@x.test>" in head and b"2024" not in head
    assert body == b"Bcc: not a header\r\n"
