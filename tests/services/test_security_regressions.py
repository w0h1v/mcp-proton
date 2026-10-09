"""Regression tests for findings from the adversarial security review."""

import contextlib

import pytest
from pydantic import ValidationError

from mcp_proton.bridge.imap import encode_criteria
from mcp_proton.domain.errors import MailError
from mcp_proton.domain.models import MessageHandle, SearchQuery
from mcp_proton.policy.model import Preset
from mcp_proton.services import conversations

from .conftest import ACCOUNT

INJECT = "x\r\nZ1 DELETE victim\r\nZ2 NOOP"


@pytest.mark.parametrize("field", ["subject", "body", "text", "from", "to", "keyword"])
def test_search_query_rejects_control_chars(field):
    with pytest.raises(ValidationError):
        SearchQuery.model_validate({field: INJECT})
    with pytest.raises(ValidationError):
        SearchQuery.model_validate({"header": {"X-Test": INJECT}})


def test_encoder_rejects_crlf_even_without_model_validation():
    with pytest.raises(MailError):
        encode_criteria(["SUBJECT", INJECT])
    with pytest.raises(MailError):
        encode_criteria(["HEADER", "Message-ID", b"a\r\nZ1 LOGOUT"])


def test_message_id_from_untrusted_header_cannot_inject(make_app, caller):
    app = make_app(preset=Preset.READER)
    store = app.store(ACCOUNT)
    store.create_mailbox("victim")
    raw = (b"From: Mallory <m@evil.test>\r\nTo: demouser@x.test\r\nSubject: hi\r\n"
           b"Date: Thu, 08 Oct 2026 10:00:00 +0000\r\n"
           b"Message-ID: =?utf-8?q?a=0D=0AZ1_DELETE_victim=0D=0AZ2_NOOP?=\r\n"
           b"Content-Type: text/plain\r\n\r\nhello\r\n")
    r = store.append("INBOX", raw)
    h = MessageHandle(account=ACCOUNT, mailbox="INBOX", uidvalidity=r.uidvalidity,
                      uid=r.uid).token()
    with contextlib.suppress(MailError):
        conversations.get_conversation(app, caller, h, mailboxes=["INBOX"])
    assert "victim" in {m.name for m in store.list_mailboxes()}


def test_normalize_id_never_falls_back_to_raw_text():
    assert conversations.normalize_id("a\r\nZ1 DELETE x") is None
    assert conversations.normalize_id("<Abc@x.test>") == "abc@x.test"


# ---------------------------------------------------------------- F2: INBOX case variants

def _deny_inbox_rules():
    from mcp_proton.domain.families import OperationFamily as F
    from mcp_proton.policy.model import Action, PolicyRule

    return [PolicyRule(family=F.READ, action=Action.DENY, mailbox="INBOX"),
            PolicyRule(family=F.ORGANIZE, action=Action.DENY, mailbox="INBOX")]


@pytest.mark.parametrize("variant", ["inbox", "Inbox", "iNbOx"])
def test_inbox_case_variants_cannot_dodge_mailbox_rules(make_app, caller, variant):
    from mcp_proton.services import mailboxes, messages

    from .conftest import make_raw

    app = make_app(rules=_deny_inbox_rules())
    r = app.store(ACCOUNT).append("INBOX", make_raw(subject="secret"))
    with pytest.raises(MailError):
        messages.list_messages(app, caller, ACCOUNT, "INBOX")
    with pytest.raises(MailError):
        messages.list_messages(app, caller, ACCOUNT, variant)
    with pytest.raises(MailError):
        messages.search(app, caller, ACCOUNT, SearchQuery(), mailboxes=[variant])
    with pytest.raises(MailError):
        mailboxes.status(app, caller, ACCOUNT, variant)
    forged = MessageHandle(account=ACCOUNT, mailbox=variant, uidvalidity=r.uidvalidity,
                           uid=r.uid).token()
    with pytest.raises(MailError):
        messages.get_message(app, caller, forged)
    with pytest.raises(MailError):
        messages.trash(app, caller, [forged])
    # raw (hand-built) token with a non-canonical mailbox is canonicalized too
    assert MessageHandle.parse(forged).mailbox == "INBOX"


def test_unknown_mailbox_is_not_found(app, caller):
    from mcp_proton.domain.errors import ErrorCode
    from mcp_proton.services import messages

    with pytest.raises(MailError) as e:
        messages.list_messages(app, caller, ACCOUNT, "NoSuchBox")
    assert e.value.code is ErrorCode.NOT_FOUND
    assert messages.list_messages(app, caller, ACCOUNT, "inbox").total == 0


def test_engine_treats_inbox_rule_patterns_case_insensitively():
    from mcp_proton.policy.engine import _mailbox_matches

    assert _mailbox_matches("INBOX", "Inbox") and _mailbox_matches("inbox", "INBOX")
    assert _mailbox_matches("inbox/*", "INBOX")
    assert not _mailbox_matches("INBOX", "inbox2") and not _mailbox_matches("Archive", "archive")


# ---------------------------------------------------------------- F3: search oracle

CONTENT_QUERIES = [
    {"body": "code is 9"}, {"text": "code"}, {"header": {"X-Test": "1"}},
    {"any_of": [{"subject": "a"}, {"body": "x"}]},
    {"not": {"text": "x"}}, {"any_of": [{"not": {"any_of": [{"header": {"A": "b"}}]}}]},
]


@pytest.mark.parametrize("raw", CONTENT_QUERIES)
def test_content_search_refused_when_bodies_not_released(make_app, caller, raw):
    from mcp_proton.domain.errors import ErrorCode
    from mcp_proton.policy.model import Constraints
    from mcp_proton.services import messages

    app = make_app(constraints=Constraints(release_bodies=False, release_attachments=False))
    q = SearchQuery.model_validate(raw)
    with pytest.raises(MailError) as e:
        messages.search(app, caller, ACCOUNT, q)
    assert e.value.code is ErrorCode.CONSTRAINT_VIOLATION
    with pytest.raises(MailError) as e2:
        messages.bulk_preview(app, caller, ACCOUNT, "archive", query=q)
    assert e2.value.code is ErrorCode.CONSTRAINT_VIOLATION


def test_metadata_search_and_client_level_restriction(make_app, caller):
    from mcp_proton.domain.errors import ErrorCode
    from mcp_proton.index import search as isearch
    from mcp_proton.policy.model import ClientConfig, Constraints
    from mcp_proton.services import messages

    from .conftest import make_raw

    app = make_app(clients=[ClientConfig(client_id="hermes",
                                         constraints=Constraints(release_bodies=False))])
    app.store(ACCOUNT).append("INBOX", make_raw(subject="findme", body="secret 123"))
    assert messages.search(app, caller, ACCOUNT, SearchQuery(subject="findme")).total == 1
    with pytest.raises(MailError) as e:
        messages.search(app, caller, ACCOUNT, SearchQuery(body="secret"))
    assert e.value.code is ErrorCode.CONSTRAINT_VIOLATION
    # saved structured searches go through the same check when run
    isearch.save_search(app, caller, ACCOUNT, "oracle", SearchQuery(body="secret"))
    with pytest.raises(MailError):
        isearch.run_saved_search(app, caller, ACCOUNT, "oracle")


# ---------------------------------------------------------------- F4: approval shows content

def test_draft_send_approval_carries_reviewable_content(make_app, caller, owner, tmp_path):
    from mcp_proton.domain.families import OperationFamily as F
    from mcp_proton.domain.models import Address, LocalAttachment, OperationStatus, OutgoingMessage
    from mcp_proton.mcp import review
    from mcp_proton.policy.model import Action, PolicyRule
    from mcp_proton.services import drafts
    from mcp_proton.services.common import review_text

    app = make_app(Preset.ASSISTANT, rules=[
        PolicyRule(family=F.ATTACHMENT_INGEST, action=Action.ALLOW)])
    f = tmp_path / "ingest" / "plan.txt"
    f.write_text("top secret plan")
    d = drafts.create_draft(app, caller, OutgoingMessage(
        account=ACCOUNT, to=[Address(email="bob@example.com")], subject="Hi",
        text="the visible body", html="<p>html <b>part</b></p>",
        attachments=[LocalAttachment(path=str(f))]))
    assert d.status is OperationStatus.SUCCEEDED
    o = drafts.send_draft(app, caller, d.result["handle"])
    assert o.status is OperationStatus.PENDING
    rec = app.journal.get(o.operation_id)
    r = rec.request.payload["review"]
    assert "the visible body" in r["text"] and "html" in r["html_text"]
    [a] = r["attachments"]
    assert a["filename"] == "plan.txt" and a["size"] == len(b"top secret plan")
    import hashlib
    assert a["sha256"] == hashlib.sha256(b"top secret plan").hexdigest()
    assert "plan.txt" in review_text(rec.request.payload)
    msg = review.describe(rec)
    assert "the visible body" in msg and "plan.txt" in msg
    # the content is bound by the digest the owner approves
    tampered = rec.request.model_copy(deep=True)
    tampered.payload["review"]["text"] = "something else"
    assert tampered.digest() != rec.digest


def test_forward_as_attachment_approval_shows_forwarded_message(make_app, caller, seed):
    from mcp_proton.domain.models import OperationStatus
    from mcp_proton.mcp import review
    from mcp_proton.services import sending

    app = make_app(Preset.ASSISTANT)
    [h] = seed(subject="Quarterly numbers", body="confidential figures")
    o = sending.forward(app, caller, h, ["bob@example.com"], as_attachment=True)
    assert o.status is OperationStatus.PENDING
    rec = app.journal.get(o.operation_id)
    fr = rec.request.payload["forward"]["review"]
    assert fr["subject"] == "Quarterly numbers" and "confidential figures" in fr["text"]
    assert "confidential figures" in review.describe(rec)


# ---------------------------------------------------------------- F5: recipient strictness

@pytest.mark.parametrize("bad", [
    "evil@evil.com\t@example.com", "a@b@example.com", "a b@example.com", "a\x00@example.com",
    'a"@example.com', "a;b@example.com", "a(c)@example.com", "a:b@example.com",
    "a@exa mple.com", "@example.com", "a@", "a@-bad.com", "a@exa_mple.com", "a@example..com",
    "a<b@example.com", "a@example.com\r\nBcc: x@y.z",
])
def test_address_rejects_malformed_addr_spec(bad):
    from mcp_proton.domain.models import Address

    with pytest.raises(ValidationError):
        Address(email=bad)


def test_address_accepts_normal_and_idna_forms():
    from mcp_proton.domain.models import Address

    for ok in ("a@example.com", "first.last+tag@sub.example.co.uk", "u@xn--bcher-kva.example"):
        assert Address(email=ok).email == ok


def test_recipient_allow_list_matches_exact_domain_and_address():
    from mcp_proton.policy.engine import _recipient_allowed

    allowed = ["@example.com", "Carol@Other.org"]
    assert _recipient_allowed("Bob@Example.COM", allowed)
    assert _recipient_allowed("carol@other.org", allowed)
    assert not _recipient_allowed("bob@sub.example.com", allowed)  # exact domain only
    assert not _recipient_allowed("bob@notexample.com", allowed)
    assert not _recipient_allowed("evil@evil.com\t@example.com", allowed)
    assert not _recipient_allowed("a@b@example.com", allowed)


# ---------------------------------------------------------------- F6: path check before I/O

def test_import_outside_roots_never_touches_the_file(app, caller, tmp_path, monkeypatch):
    from mcp_proton.domain.errors import ErrorCode
    from mcp_proton.services import transfer

    outside = tmp_path / "secret.eml"
    outside.write_bytes(b"Subject: x\r\n\r\nbody\r\n")

    def boom(*a, **k):
        raise AssertionError("file touched before path policy check")

    monkeypatch.setattr(transfer, "_file_sha256", boom)
    monkeypatch.setattr(transfer, "iter_mbox", boom)
    for fn, arg in ((transfer.import_eml, [str(outside)]), (transfer.import_mbox, str(outside)),
                    (transfer.import_eml, [str(tmp_path)])):
        with pytest.raises(MailError) as e:
            fn(app, caller, ACCOUNT, arg, "INBOX")
        assert e.value.code is ErrorCode.PATH_REJECTED
    missing = tmp_path / "nope.eml"  # existence must not leak either
    with pytest.raises(MailError) as e:
        transfer.import_eml(app, caller, ACCOUNT, [str(missing)], "INBOX")
    assert e.value.code is ErrorCode.PATH_REJECTED


def test_local_attachment_outside_ingest_roots_is_not_read(app, caller, tmp_path, monkeypatch):
    from mcp_proton.domain.errors import ErrorCode
    from mcp_proton.domain.models import LocalAttachment
    from mcp_proton.services import attachments as att

    outside = tmp_path / "id_rsa"
    outside.write_text("PRIVATE")
    monkeypatch.setattr(att, "_read_limited", lambda *a, **k: pytest.fail("file was read"))
    for p in (outside, tmp_path / "does-not-exist"):
        with pytest.raises(MailError) as e:
            att.build_manifest(app, caller, [LocalAttachment(path=str(p))], ACCOUNT)
        assert e.value.code is ErrorCode.PATH_REJECTED


def test_client_import_roots_are_intersected(make_app, caller, tmp_path):
    from mcp_proton.domain.errors import ErrorCode
    from mcp_proton.policy.model import ClientConfig, Constraints
    from mcp_proton.services import transfer

    narrow = tmp_path / "import" / "narrow"
    app = make_app(clients=[ClientConfig(client_id="hermes", constraints=Constraints(
        import_dirs=[str(narrow)]))])
    narrow.mkdir()
    f = tmp_path / "import" / "m.eml"
    f.write_bytes(b"Subject: x\r\n\r\nbody\r\n")
    with pytest.raises(MailError) as e:
        transfer.import_eml(app, caller, ACCOUNT, [str(f)], "INBOX")
    assert e.value.code is ErrorCode.PATH_REJECTED


# ---------------------------------------------------------------- F8: daily send limit

def _send_out(i=0):
    from mcp_proton.domain.models import Address, OutgoingMessage

    return OutgoingMessage(account=ACCOUNT, to=[Address(email="bob@example.com")],
                           subject=f"m{i}", text="x")


def test_daily_send_limit_holds_under_concurrency(make_app, caller, smtp_server):
    import threading

    from mcp_proton.domain.models import OperationStatus
    from mcp_proton.policy.model import Constraints
    from mcp_proton.services import sending

    app = make_app(constraints=Constraints(max_sends_per_day=3))
    results: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            o = sending.send(app, caller, _send_out(i))
            outcome = o.status.value
        except MailError as exc:
            outcome = exc.code.value
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(OperationStatus.SUCCEEDED.value) == 3, results
    assert results.count("limit_exceeded") == 5, results
    assert len(smtp_server.messages) == 3


def test_client_send_limit_counts_only_that_client(make_app, caller, smtp_server):
    from mcp_proton.domain.errors import ErrorCode
    from mcp_proton.domain.models import OperationStatus
    from mcp_proton.domain.requests import CallerContext
    from mcp_proton.policy.model import ClientConfig, Constraints
    from mcp_proton.services import sending

    app = make_app(clients=[ClientConfig(client_id="hermes",
                                         constraints=Constraints(max_sends_per_day=1))])
    other = CallerContext(client_id="other")
    for i in range(3):
        assert sending.send(app, other, _send_out(i)).status is OperationStatus.SUCCEEDED
    assert sending.send(app, caller, _send_out(9)).status is OperationStatus.SUCCEEDED
    with pytest.raises(MailError) as e:
        sending.send(app, caller, _send_out(10))
    assert e.value.code is ErrorCode.LIMIT_EXCEEDED


def test_global_send_limit_counts_every_client(make_app, caller, smtp_server):
    from mcp_proton.domain.errors import ErrorCode
    from mcp_proton.domain.requests import CallerContext
    from mcp_proton.policy.model import Constraints
    from mcp_proton.services import sending

    app = make_app(constraints=Constraints(max_sends_per_day=2))
    sending.send(app, CallerContext(client_id="other"), _send_out(0))
    sending.send(app, caller, _send_out(1))
    with pytest.raises(MailError) as e:
        sending.send(app, caller, _send_out(2))
    assert e.value.code is ErrorCode.LIMIT_EXCEEDED


# ---------------------------------------------------------------- F9: artifact scoping

def test_artifacts_are_scoped_to_their_client(app, caller, owner):
    from email.message import EmailMessage

    from mcp_proton.domain.errors import ErrorCode
    from mcp_proton.domain.models import ArtifactAttachment
    from mcp_proton.domain.requests import CallerContext
    from mcp_proton.services import attachments as att

    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "a@example.com", "demouser@x.test", "s"
    m.set_content("hi")
    m.add_attachment(b"PDFDATA", maintype="application", subtype="pdf", filename="r.pdf")
    r = app.store(ACCOUNT).append("INBOX", m.as_bytes())
    h = MessageHandle(account=ACCOUNT, mailbox="INBOX", uidvalidity=r.uidvalidity,
                      uid=r.uid).token()
    art = att.fetch_to_artifact(app, caller, h, "2")
    items = [ArtifactAttachment(artifact_id=art["artifact_id"])]
    assert att.build_manifest(app, caller, items, ACCOUNT)[0]["size"] == 7
    assert att.build_manifest(app, owner, items, ACCOUNT)  # the owner may use any
    with pytest.raises(MailError) as e:
        att.build_manifest(app, CallerContext(client_id="other"), items, ACCOUNT)
    assert e.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(MailError) as e2:
        att.build_manifest(app, caller, items, "another-account")
    assert e2.value.code is ErrorCode.NOT_FOUND
    row = app.db.query("SELECT client_id FROM artifacts WHERE id=?", (art["artifact_id"],))[0]
    assert row["client_id"] == "hermes"
