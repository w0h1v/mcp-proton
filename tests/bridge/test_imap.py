"""ImapMailStore against pymap (an in-memory fake, NOT Bridge evidence).

pymap differences worth knowing: it lacks SPECIAL-USE/NAMESPACE/UNSELECT/LIST-STATUS,
UIDs start at 101, and it has no Bridge-style label semantics.
"""

from __future__ import annotations

import socket
import threading
import time
from datetime import datetime
from email.message import EmailMessage

import pytest

from mcp_proton.bridge import imap as imap_mod
from mcp_proton.bridge.imap import (
    ImapMailStore,
    build_search_criteria,
    encode_criteria,
    open_store,
    search_notes,
)
from mcp_proton.config import AccountConfig, Security
from mcp_proton.domain.errors import ErrorCode, MailError
from mcp_proton.domain.models import MailboxRole, MessageHandle, SearchQuery


def make_account(server, **kw) -> AccountConfig:
    return AccountConfig(
        name="t", address="demouser@x.test", username="demouser",
        secret_ref="env:MCP_PROTON_TEST_SECRET", imap_host=server.host,
        imap_port=server.port, imap_security=Security.NONE, **kw)


def mime(subject="hello", frm="Alice <alice@x.test>", to="bob@x.test", body="body text",
         msgid=None, attach=False) -> bytes:
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = frm
    m["To"] = to
    m["Cc"] = "carol@x.test"
    m["Date"] = "Thu, 01 Oct 2026 10:00:00 +0000"
    m["Message-ID"] = msgid or f"<{abs(hash((subject, body)))}@x.test>"
    m.set_content(body)
    if attach:
        m.add_attachment(b"pdfdata", maintype="application", subtype="pdf", filename="a.pdf")
    return m.as_bytes()


@pytest.fixture
def store(imap_server):
    s = open_store(make_account(imap_server))
    yield s
    s.close()


def seed(store, mailbox="INBOX", n=3, **kw):
    uv = None
    uids = []
    for i in range(n):
        r = store.append(mailbox, mime(subject=f"msg {i}", body=f"body {i}", **kw))
        uv, uid = r.uidvalidity, r.uid
        uids.append(uid)
    return uv, uids


# ------------------------------------------------------------------ pure


def test_criteria_leaves_and_flags():
    q = SearchQuery(**{"from": "a@x", "subject": "hi there", "seen": False, "flagged": True,
                       "larger_than": 10, "since": datetime(2026, 1, 2, 15, 0),
                       "header": {"X-Foo": "bar"}, "keyword": "todo", "uid_range": "1:5,9"})
    c = build_search_criteria(q)
    assert c[:4] == ["FROM", "a@x", "SUBJECT", "hi there"]
    assert "UNSEEN" in c and "FLAGGED" in c
    assert c[c.index("LARGER") + 1] == 10
    assert c[c.index("SINCE") + 1].isoformat() == "2026-01-02"
    assert c[c.index("HEADER"):c.index("HEADER") + 3] == ["HEADER", "X-Foo", "bar"]
    assert c[-2:] == ["UID", "1:5,9"]
    assert build_search_criteria(SearchQuery()) == []


def test_criteria_any_of_and_not():
    q = SearchQuery(any_of=[SearchQuery(subject="a"), SearchQuery(subject="b"),
                            SearchQuery(subject="c")], not_=SearchQuery(seen=True))
    c = build_search_criteria(q)
    assert c == ["OR", ["SUBJECT", "a"], ["OR", ["SUBJECT", "b"], ["SUBJECT", "c"]],
                 "NOT", ["SEEN"]]
    one = build_search_criteria(SearchQuery(any_of=[SearchQuery(subject="a")]))
    assert one == ["SUBJECT", "a"]


def test_criteria_rejects_bad_input():
    with pytest.raises(MailError):
        build_search_criteria(SearchQuery(uid_range="1;DROP"))
    with pytest.raises(MailError):
        build_search_criteria(SearchQuery(keyword="\\Seen"))


def test_encode_criteria_groups_and_literals():
    toks = encode_criteria(["OR", ["FROM", "a b"], ["SUBJECT", "héllo"], "LARGER", 5])
    # parentheses joined to their neighbours; a literal cannot carry ")" so ALL does
    assert toks == [b"OR", b"(FROM", b'"a b")', b"(SUBJECT", "héllo".encode(), b"ALL)",
                    b"LARGER", b"5"]
    assert encode_criteria(["NOT", ["OR", ["SEEN"], ["FROM", "x"]]]) == [
        b"NOT", b"(OR", b"(SEEN)", b"(FROM", b"x))"]
    assert encode_criteria(["NOT", []]) == [b"NOT", b"(ALL)"]
    assert encode_criteria(["SUBJECT", 'q"x\\']) == [b"SUBJECT", b'"q\\"x\\\\"']
    assert encode_criteria(["SUBJECT", ""]) == [b"SUBJECT", b'""']


def _wire(monkeypatch):
    import imaplib

    sent: list[bytes] = []
    real = imaplib.IMAP4.send

    def send(self, data):
        sent.append(bytes(data))
        return real(self, data)

    monkeypatch.setattr(imaplib.IMAP4, "send", send)
    return sent


@pytest.mark.parametrize("query, expect", [
    (SearchQuery(any_of=[SearchQuery(subject="alpha"), SearchQuery(subject="beta")]),
     {"alpha", "beta"}),
    (SearchQuery(not_=SearchQuery(subject="alpha")), {"beta", "gamma héllo"}),
    (SearchQuery(subject="a", not_=SearchQuery(any_of=[SearchQuery(subject="alpha"),
                                                       SearchQuery(subject="beta")])),
     {"gamma héllo"}),
    (SearchQuery(any_of=[SearchQuery(subject="héllo"), SearchQuery(subject="beta")]),
     {"beta", "gamma héllo"}),
])
def test_boolean_search_wire_format_and_results(store, monkeypatch, query, expect):
    for subject in ("alpha", "beta", "gamma héllo"):
        store.append("INBOX", mime(subject=subject))
    sent = _wire(monkeypatch)
    uv, uids, _ = store.search("INBOX", query)
    wire = b"".join(d for d in sent if b"SEARCH" in d or not d.startswith(b"A"))
    # RFC 3501: no space after "(" or before ")" (Bridge rejects them)
    assert b"( " not in wire and b" )" not in wire, wire
    got = {s.subject for s in store.fetch_summaries("INBOX", uv, uids)}
    assert got == expect


def _with_parts(subject, parts):
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = "alice@x.test"
    m["To"] = "bob@x.test"
    m["Message-ID"] = f"<{subject.replace(' ', '.')}@x.test>"
    m.set_content("see attached")
    for maintype, subtype, filename, data in parts:
        if maintype == "message":
            inner = EmailMessage()
            inner["Subject"] = "inner"
            inner.set_content("inner body")
            m.add_attachment(inner)
        else:
            m.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return m.as_bytes()


@pytest.fixture
def attachment_box(store):
    msgs = {
        "plain": [],
        "invoice": [("application", "pdf", "Invoice-2026-10.pdf", b"%PDF")],
        "photo": [("image", "jpeg", "beach.jpg", b"\xff\xd8")],
        "both": [("application", "pdf", "contract.pdf", b"%PDF"),
                 ("image", "png", "signature.png", b"\x89PNG")],
        "unicode": [("application", "pdf", "Rechnung März.pdf", b"%PDF")],
        "forwarded": [("message", "rfc822", None, None)],
    }
    for subject, parts in msgs.items():
        store.append("INBOX", _with_parts(f"att {subject}", parts))
    return store


def _subjects(store, query):
    uv, uids, notes = store.search("INBOX", query)
    return {s.subject.removeprefix("att ") for s in store.fetch_summaries("INBOX", uv, uids)}, notes


@pytest.mark.parametrize("filters, expect", [
    ({"has_attachment": True}, {"invoice", "photo", "both", "unicode", "forwarded"}),
    ({"has_attachment": False}, {"plain"}),
    ({"attachment_type": "application/pdf"}, {"invoice", "both", "unicode"}),
    ({"attachment_type": "image/"}, {"photo", "both"}),
    ({"attachment_type": "message/rfc822"}, {"forwarded"}),
    ({"attachment_name": "invoice"}, {"invoice"}),
    ({"attachment_name": "MÄRZ"}, {"unicode"}),  # RFC 2231 filename, case-insensitive
    ({"attachment_name": ".pdf", "attachment_type": "image/"}, {"both"}),
    ({"attachment_type": "application/pdf", "subject": "contract"}, set()),  # AND-ed
    ({"attachment_type": "application/pdf", "subject": "att"},
     {"invoice", "both", "unicode"}),
])
def test_attachment_filters(attachment_box, filters, expect):
    got, notes = _subjects(attachment_box, SearchQuery(**filters))
    assert got == expect
    assert any("MIME structure" in n for n in notes)


def test_attachment_parts_from_rfc3501_structure():
    """The form Bridge sends: the disposition as ("attachment" ("filename" ...)),
    with RFC 2231 and RFC 2047 encoded names."""
    text = (b"text", b"plain", (b"charset", b"utf-8"), None, None, b"7bit", 10, 1,
            None, None, None, None)
    pdf = (b"application", b"pdf", None, None, None, b"base64", 100, None,
           (b"attachment", (b"filename*", b"utf-8''Rechnung%20M%C3%A4rz.pdf")), None, None)
    img = (b"image", b"png", (b"name", b"=?utf-8?q?R=C3=A9sum=C3=A9.png?="), None, None,
           b"base64", 100, None, (b"inline", None), None, None)
    structure = ([text, pdf, img], b"mixed", (b"boundary", b"x"), None, None, None)
    assert imap_mod._attachment_parts(structure) == [
        ("application/pdf", "Rechnung März.pdf"), ("image/png", "Résumé.png")]
    assert imap_mod._has_attachments(structure) is True
    assert imap_mod._attachments_match(structure, SearchQuery(attachment_name="résumé"))
    assert imap_mod._attachments_match(structure, SearchQuery(attachment_type="image/"))
    assert not imap_mod._attachments_match(structure, SearchQuery(attachment_type="video/"))
    assert not imap_mod._attachments_match(None, SearchQuery(has_attachment=False))


def test_attachment_parts_review_cases():
    text = (b"text", b"plain", None, None, None, b"7bit", 10, 1, None, None, None, None)
    # message/rfc822: index 7 is the envelope (a tuple starting with a byte date), the
    # disposition is at index 11
    envelope = (b"Thu, 01 Oct 2026 10:00:00 +0000", b"inner", None, None, None, None,
                None, None, None, b"<i@x>")
    inner = (b"text", b"plain", None, None, None, b"7bit", 5, 1)
    fwd = (b"message", b"rfc822", None, None, None, b"7bit", 300, envelope, inner, 12, None,
           (b"attachment", (b"filename", b"forwarded.eml")), None, None)
    # a PDF with neither filename nor disposition still counts, as attachments_list does
    bare_pdf = (b"application", b"pdf", None, None, None, b"base64", 100, None, None,
                None, None)
    # RFC 2231 continuation: filename*0* / filename*1*
    long_name = (b"application", b"zip", None, None, None, b"base64", 100, None,
                 (b"attachment", (b"filename*0*", b"utf-8''Very%20long%20",
                                  b"filename*1*", b"na%C3%AFve.zip")), None, None)
    structure = ([text, fwd, bare_pdf, long_name], b"mixed", None, None, None, None)
    assert imap_mod._attachment_parts(structure) == [
        ("message/rfc822", "forwarded.eml"), ("application/pdf", None),
        ("application/zip", "Very long naïve.zip")]
    match = imap_mod._attachments_match
    assert match(structure, SearchQuery(attachment_name="forwarded"))
    assert match(structure, SearchQuery(attachment_type="application/pdf"))
    assert match(structure, SearchQuery(attachment_name="NAÏVE"))
    only_text = ([text, text], b"alternative", None, None, None, None)
    assert imap_mod._attachment_parts(only_text) == []


def test_attachment_filters_are_top_level_only():
    from pydantic import ValidationError

    for bad in ({"any_of": [{"attachment_name": "x"}, {"subject": "y"}]},
                {"not": {"has_attachment": True}},
                {"not": {"any_of": [{"attachment_type": "image/"}, {"subject": "y"}]}}):
        with pytest.raises(ValidationError):
            SearchQuery.model_validate(bad)
    with pytest.raises(ValidationError):
        SearchQuery(attachment_type="pdf")  # not a MIME type


def test_attachment_filter_refuses_too_many_matches(attachment_box, monkeypatch):
    monkeypatch.setattr(imap_mod, "ATTACHMENT_FILTER_MAX", 2)
    with pytest.raises(MailError) as e:
        attachment_box.search("INBOX", SearchQuery(has_attachment=True))
    assert e.value.code is ErrorCode.LIMIT_EXCEEDED


def test_search_notes():
    q = SearchQuery(since=datetime(2026, 1, 1), any_of=[SearchQuery(body="x"),
                                                         SearchQuery(subject="ü")])
    notes = " ".join(search_notes(q))
    assert "day granularity" in notes and "local index" in notes and "UTF-8" in notes
    assert search_notes(SearchQuery(subject="a")) == []


def test_uid_set_helpers():
    assert imap_mod._uid_set([5, 1, 2, 3, 9, 3]) == "1:3,5,9"
    assert imap_mod._expand_uid_set("1:3,7") == [1, 2, 3, 7]


def test_roles_by_name_and_special_use(imap_server):
    s = ImapMailStore(make_account(imap_server), lambda: "x")
    r = s._role
    assert r("INBOX", [], "/") is MailboxRole.INBOX
    assert r("All Mail", [], "/") is MailboxRole.ALL_MAIL
    assert r("Spam", [], "/") is MailboxRole.SPAM
    assert r("Folders", ["\\Noselect"], "/") is MailboxRole.CONTAINER
    assert r("Folders/Work", [], "/") is MailboxRole.FOLDER
    assert r("Labels/Important", [], "/") is MailboxRole.LABEL
    assert r("Weird", [], "/") is MailboxRole.OTHER
    assert r("Weird", ["\\Noselect"], "/") is MailboxRole.CONTAINER
    assert r("Whatever", ["\\Junk"], "/") is MailboxRole.SPAM  # SPECIAL-USE wins
    assert r("Whatever", ["\\Flagged"], "/") is MailboxRole.STARRED


def test_attachment_heuristic():
    plain = ([(b"text", b"plain", None, None, None, b"7bit", 1, 1, None, None, None, None),
              (b"text", b"html", None, None, None, b"7bit", 1, 1, None, None, None, None)],
             b"alternative", None, None, None, None)
    mixed = ([(b"text", b"plain", None, None, None, b"7bit", 1, 1, None, None, None, None),
              (b"application", b"pdf", None, None, None, b"base64", 1, None, None, None, None)],
             b"mixed", None, None, None, None)
    assert imap_mod._has_attachments(plain) is False
    assert imap_mod._has_attachments(mixed) is True
    assert imap_mod._has_attachments(None) is None


def test_pool_size_bounded_and_plaintext_refused(imap_server):
    acct = make_account(imap_server).model_copy(update={"imap_host": "example.org"})
    with pytest.raises(MailError) as e:
        ImapMailStore(acct, lambda: "x").has_capability("IDLE")
    assert e.value.code is ErrorCode.TLS_ERROR


# ------------------------------------------------------------------ live


def test_auth_failure_and_unreachable(imap_server):
    s = ImapMailStore(make_account(imap_server), lambda: "wrong")
    with pytest.raises(MailError) as e:
        s.list_mailboxes()
    assert e.value.code is ErrorCode.AUTH_FAILED
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    acct = make_account(imap_server).model_copy(update={"imap_port": port})
    with pytest.raises(MailError) as e2:
        ImapMailStore(acct, lambda: "x").list_mailboxes()
    assert e2.value.code is ErrorCode.BRIDGE_UNAVAILABLE


def test_capability_report(store):
    rep = store.capabilities()
    assert rep.account == "t"
    assert "IMAP4REV1" in rep.server_capabilities
    assert rep.features["UIDPLUS"] == "available"
    assert rep.features["MOVE"] == "available"
    assert rep.features["IDLE"] == "available"
    assert rep.features["QRESYNC"] == "unavailable"
    assert set(imap_mod.FEATURES) <= set(rep.features)
    assert rep.delimiter == "/"
    assert rep.special_folders["sent"] == "Sent"
    assert rep.special_folders["all_mail"] == "All Mail"
    assert rep.checked_at is not None
    assert any("unverified" in n for n in rep.notes)
    assert store.capabilities() is rep  # cached
    assert store.capabilities(refresh=True) is not rep
    assert store.has_capability("uidplus") and not store.has_capability("QUOTA")


def test_roles_and_listing(store):
    store.create_mailbox("Folders/Work")
    store.create_mailbox("Labels/Important")
    assert store.role_of("Folders/Work") is MailboxRole.FOLDER
    assert store.role_of("Labels/Important") is MailboxRole.LABEL
    assert store.role_of("Folders") is MailboxRole.CONTAINER
    assert store.role_of("Trash") is MailboxRole.TRASH
    assert store.mailbox_for_role(MailboxRole.TRASH) == "Trash"
    assert store.mailbox_for_role(MailboxRole.INBOX) == "INBOX"
    assert store.mailbox_for_role(MailboxRole.FOLDER) is None
    seed(store, n=2)
    boxes = {m.name: m for m in store.list_mailboxes(with_counts=True)}
    assert boxes["INBOX"].messages == 2 and boxes["INBOX"].unseen == 2
    assert boxes["INBOX"].uidvalidity and boxes["INBOX"].uidnext
    assert boxes["Folders/Work"].role is MailboxRole.FOLDER
    assert boxes["Sent"].messages == 0
    assert boxes["INBOX"].subscribed is True
    plain = {m.name: m for m in store.list_mailboxes()}
    assert plain["INBOX"].messages is None


def test_mailbox_status(store):
    seed(store, n=1)
    st = store.mailbox_status("INBOX")
    assert st.messages == 1 and st.unseen == 1 and st.writable is True
    assert "\\seen" in st.permanent_flags
    assert st.role is MailboxRole.INBOX
    with pytest.raises(MailError) as e:
        store.mailbox_status("Nope")
    assert e.value.code is ErrorCode.NOT_FOUND


def test_summaries_do_not_set_seen(store):
    uv, uids = seed(store, n=2)
    raw = mime(subject="Café =?utf-8?q?x?=", attach=True, msgid="<att@x.test>")
    r = store.append("INBOX", raw)
    uids.append(r.uid)
    sums = store.fetch_summaries("INBOX", uv, uids)
    assert [s.uid for s in sums] == uids
    s0 = sums[0]
    assert s0.subject == "msg 0"
    assert s0.from_[0].email == "alice@x.test" and s0.from_[0].name == "Alice"
    assert s0.to[0].email == "bob@x.test" and s0.cc[0].email == "carol@x.test"
    assert s0.date is not None and s0.internal_date is not None and s0.size
    assert s0.has_attachments is False and sums[2].has_attachments is True
    assert MessageHandle.parse(s0.handle).uid == uids[0]
    assert all("\\Seen" not in s.flags for s in sums)
    store.fetch_message("INBOX", uv, uids[0])
    store.fetch_message("INBOX", uv, uids[0], header_only=True)
    assert store.fetch_flags("INBOX", uv, uids) == {u: [] for u in uids}
    # order follows the request, missing uids are skipped
    rev = store.fetch_summaries("INBOX", uv, [uids[1], 99999, uids[0]])
    assert [s.uid for s in rev] == [uids[1], uids[0]]
    assert store.fetch_summaries("INBOX", uv, []) == []


def test_fetch_message_raw(store):
    uv, uids = seed(store, n=1)
    full = store.fetch_message("INBOX", uv, uids[0])
    assert b"body 0" in full.raw and not full.header_only
    assert full.size and full.mailbox == "INBOX" and full.uid == uids[0]
    head = store.fetch_message("INBOX", uv, uids[0], header_only=True)
    assert head.header_only and b"Subject: msg 0" in head.raw and b"body 0" not in head.raw
    with pytest.raises(MailError) as e:
        store.fetch_message("INBOX", uv, 99999)
    assert e.value.code is ErrorCode.NOT_FOUND


def test_stale_uidvalidity(store):
    uv, uids = seed(store, n=1)
    for call in (
        lambda: store.fetch_summaries("INBOX", uv + 1, uids),
        lambda: store.fetch_message("INBOX", uv + 1, uids[0]),
        lambda: store.fetch_flags("INBOX", uv + 1, uids),
        lambda: store.store_flags("INBOX", uv + 1, uids, add=["\\Seen"]),
        lambda: store.copy("INBOX", uv + 1, uids, "Archive"),
        lambda: store.move("INBOX", uv + 1, uids, "Archive"),
        lambda: store.expunge_uids("INBOX", uv + 1, uids),
    ):
        with pytest.raises(MailError) as e:
            call()
        assert e.value.code is ErrorCode.STALE_HANDLE
    assert store.fetch_flags("INBOX", uv, uids) == {uids[0]: []}  # untouched


def test_live_search(store):
    uv, _ = seed(store, n=0)
    a = store.append("INBOX", mime(subject="Invoice 42", frm="Bob <bob@shop.test>",
                                   body="please pay"), flags=["\\Flagged"])
    b = store.append("INBOX", mime(subject="Lunch", body="tacos"))
    c = store.append("INBOX", mime(subject="Grüße", body="hallo welt"))
    uv = a.uidvalidity

    def find(**kw):
        got_uv, uids, notes = store.search("INBOX", SearchQuery(**kw))
        assert got_uv == uv
        return uids, notes

    assert find(subject="invoice")[0] == [a.uid]
    assert find(**{"from": "shop.test"})[0] == [a.uid]
    assert find(flagged=True)[0] == [a.uid]
    assert find(flagged=False)[0] == [b.uid, c.uid]
    assert find(body="tacos")[0] == [b.uid]
    assert find(header={"Message-ID": "x.test"})[0] == [a.uid, b.uid, c.uid]
    assert find(any_of=[SearchQuery(subject="Invoice"), SearchQuery(subject="Lunch")])[0] \
        == [a.uid, b.uid]
    assert find(not_=SearchQuery(subject="Invoice"))[0] == [b.uid, c.uid]
    assert find(subject="Grüße")[0] == [c.uid]
    assert find(any_of=[SearchQuery(subject="Grüße"), SearchQuery(body="tacos")])[0] \
        == [b.uid, c.uid]
    assert find(uid_range=f"{b.uid}:*")[0] == [b.uid, c.uid]
    assert find(since=datetime(2020, 1, 1))[0] == [a.uid, b.uid, c.uid]
    uids, notes = find(since=datetime(2020, 1, 1), body="tacos")
    assert uids == [b.uid] and len(notes) == 2
    assert find()[0] == [a.uid, b.uid, c.uid]
    assert store.list_uids("INBOX")[1] == [a.uid, b.uid, c.uid]
    assert store.list_uids("INBOX", ["SUBJECT", "two words"])[1] == []
    assert store.find_by_message_id("INBOX", mime_id(store, b.uid)) == (uv, [b.uid])
    assert store.find_by_message_id("INBOX", "<nope@x>") == (uv, [])


def mime_id(store, uid):
    uv = store.mailbox_status("INBOX").uidvalidity
    return store.fetch_summaries("INBOX", uv, [uid])[0].message_id


def test_store_flags_and_permanentflags(store):
    uv, uids = seed(store, n=2)
    out = store.store_flags("INBOX", uv, uids, add=["\\Seen", "\\Flagged"])
    assert all({"\\Seen", "\\Flagged"} <= set(f) for f in out.values())
    out = store.store_flags("INBOX", uv, [uids[0]], remove=["\\Flagged"])
    assert "\\Flagged" not in out[uids[0]] and "\\Seen" in out[uids[0]]
    assert store.store_flags("INBOX", uv, [], add=["\\Seen"]) == {}
    assert store.store_flags("INBOX", uv, uids[:1]) == {uids[0]: out[uids[0]]}
    # pymap advertises PERMANENTFLAGS without \*, so keywords must be refused
    with pytest.raises(MailError) as e:
        store.store_flags("INBOX", uv, uids, add=["Todo"])
    assert e.value.code is ErrorCode.UNSUPPORTED
    with pytest.raises(MailError) as e:
        store.store_flags("INBOX", uv, uids, add=["\\Recent"])
    assert e.value.code is ErrorCode.UNSUPPORTED
    with pytest.raises(MailError) as e:
        store.store_flags("INBOX", uv, uids, add=["bad flag"])
    assert e.value.code is ErrorCode.INVALID_REQUEST


def test_copy_and_move_mapping(store):
    uv, uids = seed(store, n=3)
    res = store.copy("INBOX", uv, uids[:2], "Archive")
    assert set(res.mapping) == set(uids[:2])
    assert all(isinstance(v, int) for v in res.mapping.values())
    assert res.dest_uidvalidity
    arch_uv, arch_uids = store.list_uids("Archive")
    assert arch_uv == res.dest_uidvalidity
    assert sorted(res.mapping.values()) == arch_uids
    assert store.list_uids("INBOX")[1] == uids  # copy leaves the source

    res2 = store.move("INBOX", uv, [uids[2]], "Trash")
    assert res2.dest_uidvalidity and isinstance(res2.mapping[uids[2]], int)
    assert store.list_uids("INBOX")[1] == uids[:2]
    assert store.list_uids("Trash")[1] == [res2.mapping[uids[2]]]
    with pytest.raises(MailError) as e:
        store.copy("INBOX", uv, uids[:1], "Missing")
    assert e.value.code is ErrorCode.NOT_FOUND
    assert store.copy("INBOX", uv, [], "Archive").mapping == {}


def test_move_fallback_uses_uid_expunge_only(store):
    uv, uids = seed(store, n=3)
    store.store_flags("INBOX", uv, [uids[2]], add=["\\Deleted"])  # unrelated \Deleted message
    store._caps = frozenset(c for c in store._ensure_caps() if c != "MOVE")
    store._idle_pool.clear()
    store._total = 0
    res = store.move("INBOX", uv, [uids[0]], "Archive")
    assert isinstance(res.mapping[uids[0]], int)
    assert "MOVE" in store.capabilities().server_capabilities  # fresh connection, real caps
    # force fallback by pretending the live connection lacks MOVE
    conn = store._idle_pool[0]
    conn.caps = frozenset(c for c in conn.caps if c != "MOVE")
    res = store.move("INBOX", uv, [uids[1]], "Archive")
    assert isinstance(res.mapping[uids[1]], int)
    assert store.list_uids("INBOX")[1] == [uids[2]]  # the other \Deleted message survived
    conn = store._idle_pool[0]
    conn.caps = frozenset(c for c in conn.caps if c not in ("MOVE", "UIDPLUS"))
    with pytest.raises(MailError) as e:
        store.move("INBOX", uv, [uids[2]], "Archive")
    assert e.value.code is ErrorCode.CAPABILITY_UNAVAILABLE
    assert store.list_uids("INBOX")[1] == [uids[2]]


def test_append_uid_and_flags_and_date(store):
    when = datetime.fromisoformat("2025-03-04T05:06:07+02:00")
    r = store.append("Drafts", mime(subject="draft"), flags=["\\Draft", "\\Seen"],
                     internal_date=when)
    assert r.uid and r.uidvalidity
    assert store.list_uids("Drafts") == (r.uidvalidity, [r.uid])
    s = store.fetch_summaries("Drafts", r.uidvalidity, [r.uid])[0]
    assert {"\\Draft", "\\Seen"} <= set(s.flags)
    assert s.internal_date == when
    with pytest.raises(MailError) as e:
        store.append("Missing", mime())
    assert e.value.code is ErrorCode.NOT_FOUND


def test_append_fallback_finds_message_id(store, monkeypatch):
    # Simulate a server without APPENDUID by hiding the response code.
    orig = ImapMailStore._exec

    def hide(self, fn, **kw):
        def wrapped(conn):
            res = fn(conn)
            return (None, None) if kw.get("what") == "append" else res
        return orig(self, wrapped, **kw)

    monkeypatch.setattr(ImapMailStore, "_exec", hide)
    seed(store, n=1)
    r = store.append("INBOX", mime(subject="findme", msgid="<findme@x.test>"))
    assert r.uid is not None and r.uidvalidity
    assert store.fetch_summaries("INBOX", r.uidvalidity, [r.uid])[0].subject == "findme"
    r2 = store.append("INBOX", mime(subject="noid").replace(b"Message-ID", b"X-Other"))
    assert r2.uid is None and r2.uidvalidity is None


def test_expunge_only_targeted(store):
    uv, uids = seed(store, n=4)
    store.store_flags("INBOX", uv, uids[:3], add=["\\Deleted"])
    gone = store.expunge_uids("INBOX", uv, [uids[0], uids[1], uids[3]])
    # uids[3] is not \Deleted so it must stay; uids[2] is \Deleted but untargeted
    assert gone == [uids[0], uids[1]]
    remaining = store.list_uids("INBOX")[1]
    assert remaining == [uids[2], uids[3]]
    assert store.fetch_flags("INBOX", uv, [uids[2]])[uids[2]] == ["\\Deleted"]
    assert store.expunge_uids("INBOX", uv, []) == []


def test_expunge_requires_uidplus(store):
    uv, uids = seed(store, n=1)
    store.has_capability("IDLE")
    store._caps = frozenset(c for c in store._caps if c != "UIDPLUS")
    with pytest.raises(MailError) as e:
        store.expunge_uids("INBOX", uv, uids)
    assert e.value.code is ErrorCode.CAPABILITY_UNAVAILABLE


def test_mailbox_management_and_subscription(store):
    store.create_mailbox("Folders/Work")
    with pytest.raises(MailError) as e:
        store.create_mailbox("Folders/Work")
    assert e.value.code is ErrorCode.CONFLICT
    uv, _ = seed(store, "Folders/Work", n=1)
    store.rename_mailbox("Folders/Work", "Folders/Done")
    names = {m.name for m in store.list_mailboxes()}
    assert "Folders/Done" in names and "Folders/Work" not in names
    with pytest.raises(MailError) as e:
        store.list_uids("Folders/Work")
    assert e.value.code is ErrorCode.NOT_FOUND
    assert store.list_uids("Folders/Done")[1]

    store.set_subscribed("Folders/Done", False)
    assert {m.name: m.subscribed for m in store.list_mailboxes()}["Folders/Done"] is False
    store.set_subscribed("Folders/Done", True)
    assert {m.name: m.subscribed for m in store.list_mailboxes()}["Folders/Done"] is True

    store.delete_mailbox("Folders/Done")
    assert "Folders/Done" not in {m.name for m in store.list_mailboxes()}
    with pytest.raises(MailError) as e:
        store.delete_mailbox("Folders/Done")
    assert e.value.code is ErrorCode.NOT_FOUND


def test_deleted_mailbox_drops_cached_selection(store):
    store.create_mailbox("Labels/Tmp")
    seed(store, "Labels/Tmp", n=1)
    store.list_uids("Labels/Tmp")
    store.delete_mailbox("Labels/Tmp")
    with pytest.raises(MailError) as e:
        store.list_uids("Labels/Tmp")
    assert e.value.code is ErrorCode.NOT_FOUND


def test_selection_is_reused(store, monkeypatch):
    uv, uids = seed(store, n=1)
    calls = []
    conn = store._idle_pool[0]
    real = conn.client.select_folder
    monkeypatch.setattr(conn.client, "select_folder",
                        lambda *a, **k: calls.append(a) or real(*a, **k))
    for _ in range(3):
        store.fetch_flags("INBOX", uv, uids)
    assert len(calls) <= 1


def test_reconnect_idempotent_read(store):
    uv, uids = seed(store, n=2)
    old = store._idle_pool[0].client
    old.socket().shutdown(socket.SHUT_RDWR)
    assert store.fetch_flags("INBOX", uv, uids) == {u: [] for u in uids}
    assert store._idle_pool[0].client is not old
    assert store.capabilities().features["UIDPLUS"] == "available"


def test_no_blind_retry_for_mutations(store):
    uv, uids = seed(store, n=2)
    store._idle_pool[0].client.socket().shutdown(socket.SHUT_RDWR)
    with pytest.raises(MailError) as e:
        store.copy("INBOX", uv, uids, "Archive")
    assert e.value.code is ErrorCode.BRIDGE_UNAVAILABLE
    assert e.value.details.get("outcome_unknown") is True
    # a later call reconnects transparently and nothing was copied
    assert store.list_uids("Archive")[1] == []
    store._idle_pool[0].client.socket().shutdown(socket.SHUT_RDWR)
    with pytest.raises(MailError) as e:
        store.append("INBOX", mime(subject="dup"))
    assert e.value.code is ErrorCode.BRIDGE_UNAVAILABLE
    assert len(store.list_uids("INBOX")[1]) == 2


def test_stale_pooled_connection_is_health_checked(store, monkeypatch):
    uv, uids = seed(store, n=1)
    monkeypatch.setattr(imap_mod, "HEALTH_CHECK_AFTER", 0.0)
    store._idle_pool[0].client.socket().shutdown(socket.SHUT_RDWR)
    res = store.copy("INBOX", uv, uids, "Archive")  # NOOP detects the dead socket first
    assert isinstance(res.mapping[uids[0]], int)


def test_pool_concurrency_bounded(imap_server):
    store = open_store(make_account(imap_server, pool_size=3))
    try:
        boxes = ["INBOX", "Archive", "Sent", "Drafts"]
        seeded = {box: seed(store, box, n=5) for box in boxes}
        errors: list[BaseException] = []
        seen_active = []
        active = 0
        lock = threading.Lock()
        orig = store._flags_of

        def tracked(conn, ids):
            nonlocal active
            with lock:
                active += 1
                seen_active.append(active)
            time.sleep(0.05)
            try:
                return orig(conn, ids)
            finally:
                with lock:
                    active -= 1

        store._flags_of = tracked  # type: ignore[method-assign]

        def worker(box):
            uv, uids = seeded[box]
            try:
                for _ in range(5):
                    assert store.fetch_flags(box, uv, uids) == {u: [] for u in uids}
                    assert len(store.fetch_summaries(box, uv, uids)) == 5
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(boxes[i % 4],)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        # pymap itself occasionally drops sessions that SELECT the same mailbox
        # concurrently (observed as EOF/reset); anything else is a real failure.
        assert all(isinstance(e, MailError) and e.code is ErrorCode.BRIDGE_UNAVAILABLE
                   for e in errors), errors
        assert len(errors) <= 2
        assert 1 < max(seen_active) <= 3
        assert store._total <= 3
    finally:
        store.close()


def test_idle_wait_wakes_on_new_message(store, imap_server):
    uv, _ = seed(store, n=1)
    other = open_store(make_account(imap_server))
    try:
        assert store.idle_wait("INBOX", 0.3) == []  # timeout path

        def later():
            time.sleep(0.5)
            other.append("INBOX", mime(subject="push"))

        t = threading.Thread(target=later)
        t.start()
        start = time.monotonic()
        resp = store.idle_wait("INBOX", 10)
        t.join()
        assert time.monotonic() - start < 8
        assert any(b"EXISTS" in r or "EXISTS" in r for r in resp)
        # IDLE connection is not part of the pool and still usable afterwards
        assert store._total <= 1
        assert len(store.list_uids("INBOX")[1]) == 2
        assert store.idle_wait("INBOX", 0.2) is not None
    finally:
        other.close()


def test_idle_unavailable(store):
    store.idle_wait("INBOX", 0.1)
    store._idle_conn.caps = frozenset(c for c in store._idle_conn.caps if c != "IDLE")
    with pytest.raises(MailError) as e:
        store.idle_wait("INBOX", 0.1)
    assert e.value.code is ErrorCode.CAPABILITY_UNAVAILABLE


def test_close_logs_out_everything(imap_server):
    s = open_store(make_account(imap_server))
    seed(s, n=1)
    s.idle_wait("INBOX", 0.1)
    s.close()
    with pytest.raises(MailError):
        s.list_mailboxes()
    s.close()  # idempotent
