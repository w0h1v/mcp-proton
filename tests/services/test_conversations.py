"""Conversation grouping: header threading, occurrences, heuristic fallback."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mcp_proton.domain.errors import ErrorCode, MailError
from mcp_proton.domain.models import Address, MessageHandle
from mcp_proton.policy.model import ClientConfig
from mcp_proton.services.conversations import (
    NOTE,
    HeaderInfo,
    build_threads,
    get_conversation,
    normalize_id,
    normalize_subject,
)

from .conftest import ACCOUNT


def raw(subject: str, mid: str | None, day: int, *, irt: str | None = None,
        refs: list[str] | None = None, frm: str = "alice@example.com") -> bytes:
    lines = [f"From: <{frm}>", "To: demouser@x.test", f"Subject: {subject}",
             f"Date: {day:02d} Oct 2026 10:00:00 +0000"]
    if mid:
        lines.append(f"Message-ID: <{mid}>")
    if irt:
        lines.append(f"In-Reply-To: <{irt}>")
    if refs:
        lines.append("References: " + " ".join(f"<{r}>" for r in refs))
    return ("\r\n".join(lines) + "\r\n\r\nbody\r\n").encode()


@pytest.fixture
def put(app):
    def _put(mailbox: str, data: bytes) -> str:
        r = app.store(ACCOUNT).append(mailbox, data)
        return MessageHandle(account=ACCOUNT, mailbox=mailbox, uidvalidity=r.uidvalidity,
                             uid=r.uid).token()

    return _put


SCOPE = ["INBOX", "Sent"]


def test_three_message_thread_via_headers(app, caller, put):
    a = put("INBOX", raw("Project", "a@x", 1))
    b = put("Sent", raw("Re: Project", "b@x", 2, irt="a@x", refs=["a@x"]))
    c = put("INBOX", raw("Re: Project", "c@x", 3, irt="b@x", refs=["a@x", "b@x"]))
    for seed in (a, b, c):  # any member yields the same thread
        conv = get_conversation(app, caller, seed, mailboxes=SCOPE)
        assert [n.handle for n in conv.nodes] == [a, b, c]
        assert [n.parent for n in conv.nodes] == [None, a, b]
        assert conv.method == "headers" and not conv.heuristic and not conv.incomplete
        assert conv.note == NOTE and conv.searched_mailboxes == SCOPE
        assert all(not n.heuristic for n in conv.nodes)
        assert conv.nodes[0].from_[0].email == "alice@example.com"


def test_missing_parent_is_incomplete(app, caller, put):
    b = put("INBOX", raw("Re: Gone", "b@x", 2, irt="gone@x", refs=["gone@x"]))
    conv = get_conversation(app, caller, b, mailboxes=SCOPE)
    assert conv.incomplete and conv.missing_message_ids == ["gone@x"]
    assert len(conv.nodes) == 1 and conv.nodes[0].parent is None


def test_subject_heuristic_fallback_is_flagged(app, caller, put):
    a = put("INBOX", raw("Lunch plan", "a@x", 1))
    b = put("Sent", raw("Re: Lunch plan", "b@x", 2))  # client dropped the threading headers
    put("INBOX", raw("Unrelated", "u@x", 2))
    conv = get_conversation(app, caller, a, mailboxes=SCOPE)
    assert conv.method == "subject_heuristic" and conv.heuristic
    assert [n.handle for n in conv.nodes] == [a, b]
    assert conv.nodes[1].parent == a and conv.nodes[1].heuristic


def test_seed_without_message_id_uses_heuristic(app, caller, put):
    a = put("INBOX", raw("No id", None, 1))
    b = put("Sent", raw("Re: No id", "b@x", 2))
    conv = get_conversation(app, caller, a, mailboxes=SCOPE)
    assert {n.handle for n in conv.nodes} == {a, b} and conv.method == "subject_heuristic"


def test_mixed_method(app, caller, put):
    put("INBOX", raw("Topic", "a@x", 1))
    put("Sent", raw("Re: Topic", "b@x", 2, irt="a@x", refs=["a@x"]))
    c = put("INBOX", raw("RE: Topic", "c@x", 4))  # headerless: heuristic only
    conv = get_conversation(app, caller, c, mailboxes=SCOPE)
    assert len(conv.nodes) == 3 and conv.method == "mixed"
    assert [n.heuristic for n in conv.nodes] == [False, False, True]


def test_duplicate_occurrences_form_one_logical_message(app, caller, put):
    app.store(ACCOUNT).create_mailbox("Labels/work")
    a = put("INBOX", raw("Shared", "a@x", 1))
    a_label = put("Labels/work", raw("Shared", "a@x", 1))
    b = put("INBOX", raw("Re: Shared", "b@x", 2, irt="a@x", refs=["a@x"]))
    conv = get_conversation(app, caller, b, mailboxes=["INBOX", "Labels/work"])
    assert len(conv.nodes) == 2
    node_a = next(n for n in conv.nodes if n.message_id == "<a@x>")
    assert node_a.occurrence_count == 2 and node_a.handle == a
    occ = conv.occurrences["a@x"]
    assert {o.handle for o in occ} == {a, a_label}
    assert {o.mailbox for o in occ} == {"INBOX", "Labels/work"}
    assert next(n for n in conv.nodes if n.handle == b).parent == a


def test_default_scope_prefers_all_mail(app, caller, put):
    a = put("All Mail", raw("Hello", "a@x", 1))
    b = put("All Mail", raw("Re: Hello", "b@x", 2, irt="a@x", refs=["a@x"]))
    conv = get_conversation(app, caller, b)
    assert conv.searched_mailboxes == ["All Mail"]
    assert [n.handle for n in conv.nodes] == [a, b]


def test_default_scope_without_all_mail_uses_seed_mailbox_and_sent(app, caller, put):
    app.store(ACCOUNT).delete_mailbox("All Mail")
    a = put("INBOX", raw("Hello", "a@x", 1))
    b = put("Sent", raw("Re: Hello", "b@x", 2, irt="a@x", refs=["a@x"]))
    conv = get_conversation(app, caller, a)
    assert conv.searched_mailboxes == ["INBOX", "Sent"]
    assert [n.handle for n in conv.nodes] == [a, b]


def test_limit_truncates(app, caller, put):
    a = put("INBOX", raw("T", "a@x", 1))
    for i in range(4):
        put("INBOX", raw("Re: T", f"r{i}@x", 2 + i, irt="a@x", refs=["a@x"]))
    conv = get_conversation(app, caller, a, mailboxes=SCOPE, limit=3)
    assert len(conv.nodes) == 3 and conv.truncated


def test_policy_and_stale_handles(make_app, caller, put):
    app = make_app(clients=[ClientConfig(client_id="hermes", revoked=True)])
    token = MessageHandle(account=ACCOUNT, mailbox="INBOX", uidvalidity=1, uid=1).token()
    with pytest.raises(MailError) as exc:
        get_conversation(app, caller, token)
    assert exc.value.code is ErrorCode.CLIENT_REVOKED


def test_stale_seed_handle_is_rejected(app, caller, put):
    a = MessageHandle.parse(put("INBOX", raw("T", "a@x", 1)))
    stale = MessageHandle(account=ACCOUNT, mailbox="INBOX", uidvalidity=a.uidvalidity + 1,
                          uid=a.uid).token()
    with pytest.raises(MailError) as exc:
        get_conversation(app, caller, stale, mailboxes=SCOPE)
    assert exc.value.code is ErrorCode.STALE_HANDLE


# ---------------------------------------------------------------- pure function


def hdr(n: int, mid: str | None, *, irt=None, refs=(), subject="Topic", days=0.0,
        mailbox="INBOX") -> HeaderInfo:
    base = datetime(2026, 10, 1, tzinfo=UTC)
    return HeaderInfo(handle=f"h{n}", mailbox=mailbox, uid=n,
                      message_id=f"<{mid}>" if mid else None,
                      in_reply_to=f"<{irt}>" if irt else None,
                      references=tuple(f"<{r}>" for r in refs), subject=subject,
                      date=base + timedelta(days=days),
                      from_=(Address(email="a@example.com"),))


def test_normalizers():
    assert normalize_id(" <A@Example.COM> ") == "a@example.com"
    assert normalize_id(None) is None
    assert normalize_subject("RE: Fwd:  Re[2]: Hello   World") == "hello world"


def test_build_threads_parent_prefers_in_reply_to_then_last_reference():
    res = build_threads([
        hdr(1, "a", days=0), hdr(2, "b", irt="a", refs=["a"], days=1),
        hdr(3, "c", refs=["a", "b"], days=2),  # no In-Reply-To: last reference wins
        hdr(4, "d", irt="zzz", refs=["a"], days=3),  # unknown In-Reply-To falls back to refs
    ])
    parents = {n.handle: n.parent for n in res.nodes}
    assert parents == {"h1": None, "h2": "h1", "h3": "h2", "h4": "h1"}
    assert res.incomplete and res.missing == ["zzz"]


def test_build_threads_orders_by_date_and_survives_cycles():
    res = build_threads([hdr(1, "a", irt="b", days=5), hdr(2, "b", irt="a", days=1)])
    assert [n.handle for n in res.nodes] == ["h2", "h1"]
    # a cycle never makes both nodes children of each other
    assert sum(n.parent is None for n in res.nodes) >= 1


def test_heuristic_respects_window_and_subject():
    res = build_threads([hdr(1, "a", days=0), hdr(2, "b", subject="Re: Topic", days=40),
                         hdr(3, "c", subject="Other", days=1)])
    assert all(n.parent is None for n in res.nodes) and res.method == "headers"
    res = build_threads([hdr(1, "a", days=0), hdr(2, "b", subject="Re: topic", days=29)])
    assert res.nodes[1].parent == "h1" and res.nodes[1].heuristic
    assert res.method == "subject_heuristic"


def test_same_message_id_collapses_into_occurrences():
    res = build_threads([hdr(1, "a"), hdr(2, "A", mailbox="Labels/x"), hdr(3, None, days=50)])
    assert len(res.nodes) == 2 and res.nodes[0].occurrence_count == 2
    assert [o.handle for o in res.occurrences["a"]] == ["h1", "h2"]
