"""mail_reply cannot be steered to arbitrary recipients, and recipient policy applies."""

from __future__ import annotations

from conftest import ACCOUNT, call, client_for  # type: ignore[import-not-found]
from mcp_proton.policy.model import Constraints, Preset


async def test_reply_rejects_caller_supplied_recipients(app, seed, smtp_server):
    [h] = seed(subject="Question", frm="alice@example.com")
    async with client_for(app) as c:
        out = await call(c, "mail_reply", {"handle": h, "text": "Answer",
                                           "to": [{"email": "mallory@evil.test"}]})
    assert "error" in out, out
    assert smtp_server.messages == []


async def test_reply_goes_only_to_the_original_sender(app, seed, smtp_server):
    [h] = seed(subject="Question", frm="alice@example.com")
    async with client_for(app) as c:
        out = await call(c, "mail_reply", {"handle": h, "text": "Answer", "quote": False})
    assert out["status"] == "succeeded", out
    _, rcpts, data = smtp_server.messages[0]
    assert rcpts == ["alice@example.com"]
    assert b"In-Reply-To:" in data and b"> " not in data  # threaded, nothing quoted


async def test_recipient_allow_list_applies_to_replies(make_app, smtp_server):
    app = make_app(Preset.AUTONOMOUS,
                   constraints=Constraints(allowed_recipients=["@allowed.test"]))
    from mcp_proton.bridge.imap import open_store
    from mcp_proton.domain.models import MessageHandle

    store = open_store(app.config.account(ACCOUNT))
    try:
        raw = (b"From: Alice <alice@example.com>\r\nTo: demouser@x.test\r\n"
               b"Subject: Q\r\nMessage-ID: <q1@example.com>\r\n\r\nhi\r\n")
        r = store.append("INBOX", raw)
    finally:
        store.close()
    h = MessageHandle(account=ACCOUNT, mailbox="INBOX", uidvalidity=r.uidvalidity,
                      uid=r.uid).token()
    async with client_for(app) as c:
        out = await call(c, "mail_reply", {"handle": h, "text": "Answer"})
    assert out["error"]["code"] == "constraint_violation", out
    assert smtp_server.messages == []
