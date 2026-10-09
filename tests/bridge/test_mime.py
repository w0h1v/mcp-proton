"""MIME parsing/building tests against synthetic fixtures only."""

from __future__ import annotations

import email
import email.policy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mcp_proton.bridge import mime
from mcp_proton.bridge.mime import ResolvedAttachment
from mcp_proton.bridge.ports import FetchedMessage
from mcp_proton.domain.errors import ErrorCode, MailError
from mcp_proton.domain.models import Address, Message, MessageHandle, OutgoingMessage

FIX = Path(__file__).parent.parent / "fixtures" / "mime"
HANDLE = MessageHandle(account="acct", mailbox="INBOX", uidvalidity=7, uid=42)


def raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


def parse(name: str, **kw) -> Message:
    opts = {"include_headers": False, "include_body": True, "max_body_chars": 100_000}
    opts.update(kw)
    fm = FetchedMessage("INBOX", 7, 42, ["\\Seen"], None, None, raw(name))
    return mime.parse_message(fm, HANDLE, **opts)


def stdlib(data: bytes):
    return email.message_from_bytes(data, policy=email.policy.default)


# ---------------------------------------------------------------- parsing


def test_plain_headers_and_body():
    m = parse("plain.eml", include_headers=True)
    assert m.subject == "Hello there"
    assert m.handle == HANDLE.token()
    assert m.flags == ["\\Seen"]
    assert m.from_[0].name == "Example, Alice" and m.from_[0].email == "alice@example.test"
    assert [a.email for a in m.to] == ["bob@example.test", "carol@example.test"]
    assert m.cc[0].email == "dave@example.test"
    assert m.reply_to[0].email == "support@example.test"
    assert m.message_id == "<m1@example.test>"
    assert m.in_reply_to == "<parent@example.test>"
    assert m.references == ["<root@example.test>", "<parent@example.test>"]
    assert m.date == datetime(2025, 10, 7, 10, 30, tzinfo=UTC)
    assert m.body and m.body.text.startswith("Hi Bob") and m.body.html is None
    assert m.attachments == [] and m.has_attachments is False
    assert m.headers and m.headers["Subject"] == ["Hello there"]


def test_body_and_headers_optional():
    m = parse("plain.eml", include_body=False)
    assert m.body is None and m.headers is None


def test_header_only_fetch_leaves_attachments_unknown():
    fm = FetchedMessage("INBOX", 7, 42, [], None, None, raw("mixed_attachments.eml"),
                        header_only=True)
    m = mime.parse_message(fm, HANDLE, include_headers=False, include_body=True,
                           max_body_chars=1000)
    assert m.has_attachments is None and m.attachments == []


def test_html_only_is_sanitized_and_text_derived():
    m = parse("html_only.eml")
    assert m.body.html_sanitized is True
    html = m.body.html
    for bad in ("script", "alert(", "iframe", "tracker.test", "evil.test", "onclick", "onload",
                "<form", "<input", "data:image", "javascript:", "url(", "<link", "<style"):
        assert bad not in html, bad
    assert "cid:logo@example.test" in html
    assert "<b>world</b>" in html
    assert 'rel="noopener noreferrer nofollow"' in html
    assert 'href="https://example.test/x"' in html
    assert "Big News" in m.body.text and "world & friends" in m.body.text
    assert "alert" not in m.body.text and "background" not in m.body.text


def test_body_format_selection():
    assert parse("alternative.eml", body_format="text").body.html is None
    b = parse("alternative.eml", body_format="html").body
    assert b.text is None and "version" in b.html
    b = parse("alternative.eml").body
    assert b.text == "Plain version" and "<i>version</i>" in b.html
    # html-only message asked for text still gets derived text
    assert "Big News" in parse("html_only.eml", body_format="text").body.text


def test_truncation():
    m = parse("plain.eml", max_body_chars=10)
    assert m.body.truncated is True and len(m.body.text) == 10
    assert parse("plain.eml").body.truncated is False


def test_mixed_attachments_and_rfc2231_filename():
    m = parse("mixed_attachments.eml")
    assert m.has_attachments is True
    assert [a.part_id for a in m.attachments] == ["2", "3"]
    pdf, txt = m.attachments
    assert pdf.filename == "résumé.pdf" and pdf.content_type == "application/pdf"
    assert pdf.size == len(b"%PDF-1.4 fake pdf content") and pdf.inline is False
    assert txt.filename == "notes.txt" and txt.content_type == "text/plain"
    assert m.body.text.strip() == "See attached."


def test_inline_cid_part():
    m = parse("inline_cid.eml")
    [att] = m.attachments
    assert att.part_id == "2" and att.inline is True
    assert att.content_id == "logo@example.test" and att.content_type == "image/png"
    assert att.filename == "logo.png" and att.size == 16
    assert "cid:logo@example.test" in m.body.html


def test_nested_rfc822_is_attachment_not_descended():
    m = parse("nested_rfc822.eml")
    [att] = m.attachments
    assert att.part_id == "2" and att.content_type == "message/rfc822"
    assert att.filename == "inner.eml"
    assert "inner body" not in (m.body.text or "")
    info, data = mime.get_part(raw("nested_rfc822.eml"), "2")
    assert info.part_id == "2"
    inner = stdlib(data)
    assert inner["Subject"] == "Inner subject"
    # nested parts of the attached message are not addressable
    with pytest.raises(MailError) as e:
        mime.get_part(raw("nested_rfc822.eml"), "2.1")
    assert e.value.code is ErrorCode.NOT_FOUND


def test_part_paths_for_nested_multipart():
    data = raw("mixed_attachments.eml")
    leaves = [pid for pid, _ in mime._leaves(stdlib(data))]
    assert leaves == ["1.1", "1.2", "2", "3"]
    info, content = mime.get_part(data, "1.1")
    assert content.strip() == b"See attached." and info.content_type == "text/plain"
    info, content = mime.get_part(data, "2")
    assert content == b"%PDF-1.4 fake pdf content" and info.filename == "résumé.pdf"


def test_get_part_single_part_and_not_found():
    info, content = mime.get_part(raw("plain.eml"), "1")
    assert content.startswith(b"Hi Bob")
    for bad in ("2", "0", "1.1", "abc", "", "1..2"):
        with pytest.raises(MailError) as e:
            mime.get_part(raw("plain.eml"), bad)
        assert e.value.code is ErrorCode.NOT_FOUND


@pytest.mark.parametrize(
    "name", ["malformed_no_boundary.eml", "malformed_bad_boundary.eml",
             "malformed_unterminated.eml"]
)
def test_malformed_mime_degrades(name):
    fm = FetchedMessage("INBOX", 7, 42, [], None, None, raw(name))
    m, notes = mime.parse_message_with_notes(
        fm, HANDLE, include_headers=True, include_body=True, max_body_chars=1000
    )
    assert m.subject
    assert m.body and (m.body.text or m.body.html)
    assert notes and all(n.startswith("mime defect") for n in notes)


def test_malformed_unterminated_keeps_text():
    assert "Truncated message body" in parse("malformed_unterminated.eml").body.text


@pytest.mark.parametrize("data", [b"", b"\x00\xff\xfe garbage", b"Content-Type: multipart/x\n\n--"])
def test_garbage_never_raises(data):
    fm = FetchedMessage("INBOX", 7, 42, [], None, None, data)
    m = mime.parse_message(fm, HANDLE, include_headers=True, include_body=True,
                           max_body_chars=100)
    assert isinstance(m, Message)
    with pytest.raises(MailError):
        mime.get_part(data, "5")


def test_charsets():
    assert parse("charset_latin1.eml").body.text.strip() == "Café crème brûlée"
    assert parse("charset_cyrillic.eml").body.text.strip() == "Привет, мир"
    assert parse("charset_unknown.eml").body.text.strip() == "plain ascii body"


def test_encoded_word_headers():
    m = parse("encoded_subject.eml")
    assert m.subject == "Grüße aus Zürich"
    assert m.from_[0].name == "Jörg Müller" and m.from_[0].email == "joerg@example.test"
    assert m.to[0].name == "Zoë"


def test_unparseable_addresses_skipped():
    data = (b"From: ok@example.test\r\nTo: not an address, good@example.test, <>\r\n"
            b"Subject: x\r\n\r\nb")
    fm = FetchedMessage("INBOX", 7, 42, [], None, None, data)
    m = mime.parse_message(fm, HANDLE, include_headers=False, include_body=False,
                           max_body_chars=10)
    assert "good@example.test" in [a.email for a in m.to]
    assert m.from_[0].email == "ok@example.test"


# ---------------------------------------------------------------- sanitize


def test_sanitize_html_direct():
    out = mime.sanitize_html(
        '<div style="background:url(http://x.test/a)"><img src="http://x.test/p.png">'
        '<img src="cid:a@b" alt="a"><a href="http://x.test">t</a><script>1</script>ok</div>'
    )
    assert "x.test/a" not in out and "p.png" not in out and "script" not in out
    assert 'src="cid:a@b"' in out and out.count("<img") == 1
    assert "nofollow" in out and out.endswith("ok</div>")
    assert mime.sanitize_html("") == ""


# ---------------------------------------------------------------- building

FROM = Address(name="Alice", email="alice@example.test")
MID = "<fixed123@example.test>"
DATE = datetime(2025, 10, 7, 10, 30, tzinfo=UTC)


def out(**kw) -> OutgoingMessage:
    base = {"account": "acct", "to": [Address(name="Bob", email="bob@example.test")],
            "subject": "Hi", "text": "hello"}
    base.update(kw)
    return OutgoingMessage(**base)


def build(o: OutgoingMessage, **kw) -> bytes:
    return mime.build_message(o, from_addr=FROM, message_id=MID, date=DATE, **kw)


def test_make_message_id():
    a, b = mime.make_message_id("example.test"), mime.make_message_id("example.test")
    assert a != b and a.startswith("<") and a.endswith("@example.test>")


def test_build_text_only():
    data = build(out(cc=[Address(email="c@example.test")],
                     reply_to=[Address(email="r@example.test")],
                     in_reply_to="<p@example.test>", references=["<a@x>", "<p@example.test>"]))
    m = stdlib(data)
    assert m.get_content_type() == "text/plain" and m.get_content().strip() == "hello"
    assert m["From"].addresses[0].addr_spec == "alice@example.test"
    assert m["To"].addresses[0].display_name == "Bob"
    assert m["Cc"].addresses[0].addr_spec == "c@example.test"
    assert m["Reply-To"].addresses[0].addr_spec == "r@example.test"
    assert m["Subject"] == "Hi" and m["Message-ID"] == MID
    assert m["In-Reply-To"] == "<p@example.test>"
    assert m["References"] == "<a@x> <p@example.test>"
    assert m["MIME-Version"] == "1.0"
    assert email_date(m) == DATE
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")


def email_date(m):
    return email.utils.parsedate_to_datetime(m["Date"])


def test_build_html_only_and_empty():
    m = stdlib(build(out(text=None, html="<p>x</p>")))
    assert m.get_content_type() == "text/html"
    m = stdlib(build(out(text=None, html=None)))
    assert m.get_content_type() == "text/plain" and m.get_content().strip() == ""


def test_build_alternative_non_ascii():
    m = stdlib(build(out(subject="Grüße", text="Zürich", html="<p>Zürich</p>")))
    assert m.get_content_type() == "multipart/alternative"
    assert m["Subject"] == "Grüße"
    assert m.get_body(("plain",)).get_content().strip() == "Zürich"
    assert "Zürich" in m.get_body(("html",)).get_content()


def test_build_related_and_mixed():
    png = bytes.fromhex("89504e470d0a1a0a")
    atts = [
        ResolvedAttachment("logo.png", "image/png", png, inline_cid="logo@example.test"),
        ResolvedAttachment("report.pdf", "application/pdf", b"%PDF-data", None),
    ]
    data = build(out(html='<img src="cid:logo@example.test">'), attachments=atts)
    m = stdlib(data)
    assert m.get_content_type() == "multipart/mixed"
    related = m.get_body(("html",))
    assert related is not None
    parts = {p.get_content_type() for p in m.walk()}
    assert {"multipart/related", "image/png", "application/pdf", "text/html"} <= parts
    infos = mime.list_attachments(m)
    by_name = {i.filename: i for i in infos}
    assert by_name["logo.png"].inline and by_name["logo.png"].content_id == "logo@example.test"
    assert not by_name["report.pdf"].inline
    _, content = mime.get_part(data, by_name["report.pdf"].part_id)
    assert content == b"%PDF-data"


def test_build_alternative_with_related_and_attachment_round_trip():
    atts = [ResolvedAttachment("l.png", "image/png", b"\x89PNG", "l@x"),
            ResolvedAttachment("résumé.pdf", "application/pdf", b"PDF", None)]
    data = build(out(html="<img src='cid:l@x'>"), attachments=atts)
    m = stdlib(data)
    assert m.get_body(("plain",)).get_content().strip() == "hello"
    assert [a.filename for a in mime.list_attachments(m)] == ["l.png", "résumé.pdf"]


def test_inline_cid_without_html_becomes_regular_attachment():
    data = build(out(), attachments=[ResolvedAttachment("a.png", "image/png", b"x", "a@x")])
    assert [a.filename for a in mime.list_attachments(stdlib(data))] == ["a.png"]


def test_build_forward_as_attachment():
    fwd = raw("plain.eml")
    data = build(out(), forwarded_raw=fwd)
    m = stdlib(data)
    assert m.get_content_type() == "multipart/mixed"
    [att] = mime.list_attachments(m)
    assert att.content_type == "message/rfc822"
    _, inner_bytes = mime.get_part(data, att.part_id)
    inner = stdlib(inner_bytes)
    assert inner["Subject"] == "Hello there" and inner["Message-ID"] == "<m1@example.test>"


def test_bcc_never_in_headers_unless_draft():
    o = out(bcc=[Address(email="secret@example.test")])
    assert b"secret@example.test" not in build(o)
    assert b"Bcc" not in build(o)
    draft = build(o, include_bcc_header=True)
    assert stdlib(draft)["Bcc"].addresses[0].addr_spec == "secret@example.test"


@pytest.mark.parametrize(
    "kw",
    [
        {"subject": "ok\r\nBcc: evil@example.test"},
        {"subject": "a\nb"},
        {"to": [Address(name="Bob\r\nBcc: x@y.test", email="bob@example.test")]},
        {"in_reply_to": "<a@b>\r\nX: y"},
        {"references": ["not-bracketed"]},
    ],
)
def test_header_injection_rejected(kw):
    with pytest.raises(MailError) as e:
        build(out(**kw))
    assert e.value.code is ErrorCode.INVALID_REQUEST


def test_injection_in_attachment_and_message_id_rejected():
    for att in (ResolvedAttachment("a\r\nb.txt", "text/plain", b"x"),
                ResolvedAttachment("a.txt", "text/plain\r\nX: y", b"x"),
                ResolvedAttachment("a.txt", "text/plain", b"x", "c\nid")):
        with pytest.raises(MailError) as e:
            build(out(html="<p>x</p>"), attachments=[att])
        assert e.value.code is ErrorCode.INVALID_REQUEST
    with pytest.raises(MailError):
        mime.build_message(out(), from_addr=FROM, message_id="<a@b>\r\nX: y")


def test_extract_draft_payload_round_trip():
    o = out(cc=[Address(name="Cee", email="c@example.test")],
            bcc=[Address(email="secret@example.test")], html="<p>hi</p>",
            in_reply_to="<p@example.test>", references=["<a@x>", "<p@example.test>"])
    payload = mime.extract_draft_payload(build(o, include_bcc_header=True))
    rebuilt = OutgoingMessage(account="acct", **payload)
    assert rebuilt.to == o.to and rebuilt.cc == o.cc and rebuilt.bcc == o.bcc
    assert rebuilt.subject == "Hi" and rebuilt.text.strip() == "hello"
    assert "<p>hi</p>" in rebuilt.html
    assert rebuilt.in_reply_to == "<p@example.test>"
    assert rebuilt.references == ["<a@x>", "<p@example.test>"]
    assert rebuilt.from_ == FROM


# ---------------------------------------------------------------- reply / forward


def orig(**kw) -> Message:
    base = {
        "handle": "h", "mailbox": "INBOX", "uid": 1, "message_id": "<m@x>",
        "subject": "Topic",
        "from": [Address(name="Ann", email="ann@x.test")],
        "to": [Address(email="me@x.test"), Address(email="Bob@x.test")],
        "cc": [Address(email="cc@x.test"), Address(email="BOB@x.test")],
        "date": datetime(2025, 10, 7, 10, 30, tzinfo=UTC),
        "body": {"text": "line one\n\nline three"},
    }
    base.update(kw)
    return Message.model_validate(base)


def test_reply_headers_chain_and_bound():
    irt, refs = mime.reply_headers(orig(references=["<r1@x>", "<r2@x>"]))
    assert irt == "<m@x>" and refs == ["<r1@x>", "<r2@x>", "<m@x>"]
    _, refs = mime.reply_headers(orig(references=["<m@x>", "<r2@x>"]))
    assert refs == ["<r2@x>", "<m@x>"]
    many = [f"<r{i}@x>" for i in range(40)]
    _, refs = mime.reply_headers(orig(references=many))
    assert len(refs) == 20 and refs[-1] == "<m@x>" and refs[0] == "<r21@x>"
    assert mime.reply_headers(orig(message_id=None))[0] is None


def test_subjects():
    assert mime.reply_subject("Topic") == "Re: Topic"
    assert mime.reply_subject("Re: Topic") == "Re: Topic"
    assert mime.reply_subject("RE: Topic") == "RE: Topic"
    assert mime.reply_subject(None) == "Re: "
    assert mime.forward_subject("Topic") == "Fwd: Topic"
    assert mime.forward_subject("FWD: Topic") == "FWD: Topic"
    assert mime.forward_subject("Fw: Topic") == "Fw: Topic"


def test_reply_recipients_simple_uses_reply_to():
    o = orig(reply_to=[Address(email="support@x.test")])
    to, cc = mime.reply_recipients(o, {"me@x.test"}, reply_all=False)
    assert [a.email for a in to] == ["support@x.test"] and cc == []
    to, cc = mime.reply_recipients(orig(), {"me@x.test"}, reply_all=False)
    assert [a.email for a in to] == ["ann@x.test"]


def test_reply_all_excludes_self_and_dedups():
    to, cc = mime.reply_recipients(orig(), {"ME@x.test"}, reply_all=True)
    assert [a.email for a in to] == ["ann@x.test"]
    assert [a.email.lower() for a in cc] == ["bob@x.test", "cc@x.test"]


def test_reply_to_own_sent_message_goes_to_original_to():
    o = orig(**{"from": [Address(email="me@x.test")]})
    to, cc = mime.reply_recipients(o, {"me@x.test"}, reply_all=False)
    assert [a.email for a in to] == ["Bob@x.test"] and cc == []
    to, cc = mime.reply_recipients(o, {"me@x.test"}, reply_all=True)
    assert [a.email for a in to] == ["Bob@x.test"] and [a.email for a in cc] == ["cc@x.test"]


def test_quote_and_forward_text():
    q = mime.quote_text(orig())
    assert q.startswith("On Tue, Oct 07, 2025 at 10:30, Ann wrote:\n> line one\n>\n> line three")
    f = mime.forward_inline_text(orig())
    assert f.startswith("---------- Forwarded message ----------\nFrom: Ann <ann@x.test>")
    assert "Subject: Topic" in f and "Cc: " in f and f.endswith("line three")
    html_only = orig(body={"html": "<p>Only <b>html</b></p>"})
    assert "Only html" in mime.quote_text(html_only)
    assert mime.quote_text(orig(body=None)).endswith("wrote:\n")
