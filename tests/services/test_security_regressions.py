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
