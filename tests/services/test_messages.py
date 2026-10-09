"""Message reads, search and writes through the real IMAP adapter against pymap."""

from __future__ import annotations

import pytest

from mcp_proton.domain.errors import ErrorCode, MailError
from mcp_proton.domain.families import OperationFamily as F
from mcp_proton.domain.models import MessageHandle, OperationStatus, SearchQuery
from mcp_proton.policy.model import Action, Constraints, PolicyRule, Preset
from mcp_proton.services import common, messages

from .conftest import ACCOUNT, make_raw

pytestmark = pytest.mark.usefixtures("app")


def parse(token: str) -> MessageHandle:
    return MessageHandle.parse(token)


def flags_of(app, token: str) -> list[str]:
    h = parse(token)
    return app.store(ACCOUNT).fetch_flags(h.mailbox, h.uidvalidity, [h.uid])[h.uid]


def uids_in(app, mailbox: str) -> list[int]:
    return app.store(ACCOUNT).list_uids(mailbox)[1]


def mkmailbox(app, name: str) -> None:
    app.store(ACCOUNT).create_mailbox(name)


def put(app, mailbox: str, n: int = 1, **kw) -> list[str]:
    """Like the ``seed`` fixture but through a specific app's own store."""
    out = []
    for i in range(n):
        r = app.store(ACCOUNT).append(mailbox, make_raw(subject=f"Message {i}", **kw))
        out.append(MessageHandle(account=ACCOUNT, mailbox=mailbox, uidvalidity=r.uidvalidity,
                                 uid=r.uid).token())
    return out


def subject_of(app, token: str) -> str | None:
    h = parse(token)
    s = app.store(ACCOUNT).fetch_summaries(h.mailbox, h.uidvalidity, [h.uid])
    return s[0].subject if s else None


# ------------------------------------------------------------------ reads


def test_list_messages_pages_newest_first(app, caller, seed):
    tokens = seed("INBOX", 5)
    page1 = messages.list_messages(app, caller, ACCOUNT, "INBOX", limit=2)
    assert [i.handle for i in page1.items] == [tokens[4], tokens[3]]
    assert page1.total == 5 and page1.next_cursor
    page2 = messages.list_messages(app, caller, ACCOUNT, "INBOX", limit=2, cursor=page1.next_cursor)
    assert [i.handle for i in page2.items] == [tokens[2], tokens[1]]
    page3 = messages.list_messages(app, caller, ACCOUNT, "INBOX", limit=2, cursor=page2.next_cursor)
    assert [i.handle for i in page3.items] == [tokens[0]] and page3.next_cursor is None


def test_list_messages_limit_is_bounded_by_config(app, caller, seed):
    seed("INBOX", 3)
    app.config.max_page_size = 2
    page = messages.list_messages(app, caller, ACCOUNT, "INBOX", limit=500)
    assert len(page.items) == 2 and page.next_cursor


def test_list_unread_only(app, caller, seed):
    read = seed("INBOX", 1, flags=["\\Seen"])
    unread = seed("INBOX", 2)
    page = messages.list_messages(app, caller, ACCOUNT, "INBOX", unread_only=True)
    assert {i.handle for i in page.items} == set(unread) and read[0] not in {
        i.handle for i in page.items}


def test_cursor_after_uidvalidity_change_is_stale(app, caller, seed):
    seed("INBOX", 3)
    page = messages.list_messages(app, caller, ACCOUNT, "INBOX", limit=1)
    cur = common.decode_cursor(page.next_cursor)
    cur["uv"] += 1
    with pytest.raises(MailError) as e:
        messages.list_messages(app, caller, ACCOUNT, "INBOX", limit=1,
                               cursor=common.encode_cursor(cur))
    assert e.value.code is ErrorCode.STALE_HANDLE


def test_cursor_from_another_listing_is_invalid(app, caller, seed):
    seed("INBOX", 3)
    page = messages.list_messages(app, caller, ACCOUNT, "INBOX", limit=1)
    with pytest.raises(MailError) as e:
        messages.list_messages(app, caller, ACCOUNT, "INBOX", limit=1,
                               cursor=page.next_cursor, unread_only=True)
    assert e.value.code is ErrorCode.INVALID_REQUEST


def test_get_message_does_not_set_seen(app, caller, seed):
    (t,) = seed("INBOX", 1, subject="Quarterly", body="numbers here")
    m = messages.get_message(app, caller, t, include_headers=True)
    assert m.subject == "Quarterly" and "numbers here" in (m.body.text or "")
    assert m.headers is not None and "\\Seen" not in flags_of(app, t)


def test_get_message_mark_read_is_a_separate_write(app, caller, seed):
    (t,) = seed("INBOX", 1)
    m, outcome = messages.read_message(app, caller, t, mark_read=True)
    assert outcome is not None and outcome.kind == "messages.flags"
    assert outcome.status is OperationStatus.SUCCEEDED
    assert "\\Seen" in flags_of(app, t) and "\\Seen" in m.flags


def test_mark_read_denied_for_reader_but_read_works(make_app, caller, seed):
    app = make_app(Preset.READER)
    (t,) = seed("INBOX", 1)
    assert messages.get_message(app, caller, t).body is not None
    with pytest.raises(MailError) as e:
        messages.get_message(app, caller, t, mark_read=True)
    assert e.value.code is ErrorCode.POLICY_DENIED
    assert "\\Seen" not in flags_of(app, t)


def test_release_bodies_false_hides_body(make_app, caller, seed):
    app = make_app(Preset.AUTONOMOUS, constraints=Constraints(release_bodies=False))
    (t,) = seed("INBOX", 1, subject="Visible subject", body="secret body")
    m = messages.get_message(app, caller, t)
    assert m.body is None and m.subject == "Visible subject"
    with pytest.raises(MailError) as e:
        messages.get_raw(app, caller, t)
    assert e.value.code is ErrorCode.CONSTRAINT_VIOLATION


def test_get_raw_returns_bytes(app, caller, seed):
    (t,) = seed("INBOX", 1, body="rawbody")
    raw = messages.get_raw(app, caller, t)
    assert b"rawbody" in raw and b"Subject:" in raw


def test_get_message_truncates_to_config(app, caller, seed):
    app.config.max_body_chars = 1000
    (t,) = seed("INBOX", 1, body="x" * 5000)
    m = messages.get_message(app, caller, t)
    assert m.body.truncated and len(m.body.text) <= 1000


def test_get_message_stale_handle(app, caller, seed):
    (t,) = seed("INBOX", 1)
    h = parse(t)
    bad = MessageHandle(account=h.account, mailbox=h.mailbox, uidvalidity=h.uidvalidity + 7,
                        uid=h.uid).token()
    with pytest.raises(MailError) as e:
        messages.get_message(app, caller, bad)
    assert e.value.code is ErrorCode.STALE_HANDLE


# ------------------------------------------------------------------ search


def test_search_default_scope_is_inbox(app, caller, seed):
    seed("INBOX", 1, subject="needle one")
    seed("Archive", 1, subject="needle two")
    res = messages.search(app, caller, ACCOUNT, SearchQuery(subject="needle"))
    assert res.scope == ["INBOX"] and res.total == 1
    assert res.completeness == "unknown"
    assert any("INBOX only" in n for n in res.notes)


def test_search_across_mailboxes_is_newest_first_and_pages(app, caller, seed):
    a = seed("INBOX", 2, subject="needle a")
    b = seed("Archive", 2, subject="needle b")
    q = SearchQuery(subject="needle")
    res = messages.search(app, caller, ACCOUNT, q, mailboxes=["INBOX", "Archive"], limit=3)
    assert res.scope == ["INBOX", "Archive"] and res.total == 4 and len(res.items) == 3
    assert res.next_cursor
    res2 = messages.search(app, caller, ACCOUNT, q, mailboxes=["INBOX", "Archive"], limit=3,
                           cursor=res.next_cursor)
    got = [i.handle for i in res.items] + [i.handle for i in res2.items]
    assert sorted(got) == sorted(a + b) and len(set(got)) == 4
    assert res2.next_cursor is None
    # within a mailbox the order is newest (highest UID) first
    inbox = [parse(h).uid for h in got if parse(h).mailbox == "INBOX"]
    assert inbox == sorted(inbox, reverse=True)
    assert any("Searched: INBOX, Archive" in n for n in res.notes)


def test_search_cursor_rejected_for_other_query(app, caller, seed):
    seed("INBOX", 3, subject="needle")
    res = messages.search(app, caller, ACCOUNT, SearchQuery(subject="needle"), limit=1)
    with pytest.raises(MailError) as e:
        messages.search(app, caller, ACCOUNT, SearchQuery(subject="other"), cursor=res.next_cursor)
    assert e.value.code is ErrorCode.INVALID_REQUEST


def test_search_respects_mailbox_constraint(make_app, caller, seed):
    app = make_app(Preset.AUTONOMOUS, constraints=Constraints(
        allowed_mailboxes={ACCOUNT: ["INBOX"]}))
    with pytest.raises(MailError) as e:
        messages.search(app, caller, ACCOUNT, SearchQuery(), mailboxes=["Archive"])
    assert e.value.code is ErrorCode.CONSTRAINT_VIOLATION


# ------------------------------------------------------------------ flags


def test_normalize_flags_mapping_and_refusals():
    assert messages.normalize_flags(["read", "star"], None) == (["\\Seen", "\\Flagged"], [])
    assert messages.normalize_flags(["unread"], ["star"]) == ([], ["\\Seen", "\\Flagged"])
    assert messages.normalize_flags(None, ["unread"]) == (["\\Seen"], [])
    for bad in (["\\Deleted"], ["deleted"], ["\\Recent"], ["$Phishing"]):
        with pytest.raises(MailError):
            messages.normalize_flags(bad, None)
    with pytest.raises(MailError):
        messages.normalize_flags(["read"], ["read"])
    with pytest.raises(MailError):
        messages.normalize_flags(None, None)


def test_set_flags_read_star_and_prior_state(app, caller, seed):
    t = seed("INBOX", 2)
    out = messages.set_flags(app, caller, t, add=["read", "star"])
    assert out.status is OperationStatus.SUCCEEDED and out.kind == "messages.flags"
    assert all({"\\Seen", "\\Flagged"} <= set(flags_of(app, x)) for x in t)
    assert out.summary == "Update flags (+\\Seen,+\\Flagged) on 2 messages from INBOX"
    prior = [p for _, p in app.journal.items(out.operation_id)]
    assert all(p and p["flags"] == [] and p["mailbox"] == "INBOX" for p in prior)
    messages.set_flags(app, caller, t, add=["unread"], remove=["star"])
    assert all(not ({"\\Seen", "\\Flagged"} & set(flags_of(app, x))) for x in t)


def test_set_flags_cannot_delete(app, caller, seed):
    (t,) = seed("INBOX", 1)
    with pytest.raises(MailError):
        messages.set_flags(app, caller, [t], add=["\\Deleted"])


# ------------------------------------------------------------------ filing


def test_move_gives_new_handles_and_leaves_source(app, caller, seed):
    mkmailbox(app, "Folders/Work")
    t = seed("INBOX", 2, subject="movable")
    out = messages.move(app, caller, t, "Folders/Work")
    assert out.status is OperationStatus.SUCCEEDED
    assert out.summary == "Move 2 messages from INBOX to Folders/Work"
    assert "movable" not in out.summary
    assert len(uids_in(app, "INBOX")) == 0 and len(uids_in(app, "Folders/Work")) == 2
    news = [i.new_handle for i in out.items]
    assert all(news)
    assert {subject_of(app, n) for n in news} == {"movable"}
    assert {parse(n).mailbox for n in news} == {"Folders/Work"}
    prior = [p for _, p in app.journal.items(out.operation_id)]
    assert all(p["mailbox"] == "INBOX" and p["dest"] == "Folders/Work" for p in prior)


def test_archive_trash_restore_spam_not_spam(app, caller, seed):
    (t,) = seed("INBOX", 1, subject="roundtrip")

    def only(out):
        assert out.status is OperationStatus.SUCCEEDED, out
        return out.items[0].new_handle

    a = only(messages.archive(app, caller, [t]))
    assert parse(a).mailbox == "Archive"
    tr = only(messages.trash(app, caller, [a]))
    assert parse(tr).mailbox == "Trash"
    r = only(messages.restore(app, caller, [tr]))
    assert parse(r).mailbox == "INBOX"
    s = only(messages.spam(app, caller, [r]))
    assert parse(s).mailbox == "Spam"
    n = only(messages.not_spam(app, caller, [s]))
    assert parse(n).mailbox == "INBOX" and subject_of(app, n) == "roundtrip"
    r2 = only(messages.restore(app, caller, [only(messages.trash(app, caller, [n]))],
                               dest="Archive"))
    assert parse(r2).mailbox == "Archive"


def test_move_to_unknown_destination_is_not_found(app, caller, seed):
    t = seed("INBOX", 1)
    with pytest.raises(MailError) as e:
        messages.move(app, caller, t, "Folders/Nope")
    assert e.value.code is ErrorCode.NOT_FOUND


def test_archive_skips_messages_already_archived(app, caller, seed):
    inbox = seed("INBOX", 1)
    arch = seed("Archive", 1)
    out = messages.archive(app, caller, inbox + arch)
    assert out.status is OperationStatus.PARTIALLY_SUCCEEDED
    by = {i.target: i for i in out.items}
    assert by[inbox[0]].status == "succeeded" and by[arch[0]].status == "skipped"


def test_all_in_destination_is_rejected(app, caller, seed):
    arch = seed("Archive", 2)
    with pytest.raises(MailError) as e:
        messages.archive(app, caller, arch)
    assert e.value.code is ErrorCode.INVALID_REQUEST


def test_move_from_label_mailbox_is_unsupported_semantics(app, caller, seed):
    mkmailbox(app, "Labels/Urgent")
    (t,) = seed("Labels/Urgent", 1)
    for call in (lambda: messages.move(app, caller, [t], "Archive"),
                 lambda: messages.archive(app, caller, [t]),
                 lambda: messages.trash(app, caller, [t])):
        with pytest.raises(MailError) as e:
            call()
        assert e.value.code is ErrorCode.UNSUPPORTED_SEMANTICS
    assert len(uids_in(app, "Labels/Urgent")) == 1


def test_move_into_label_mailbox_is_unsupported_semantics(app, caller, seed):
    mkmailbox(app, "Labels/Urgent")
    (t,) = seed("INBOX", 1)
    with pytest.raises(MailError) as e:
        messages.move(app, caller, [t], "Labels/Urgent")
    assert e.value.code is ErrorCode.UNSUPPORTED_SEMANTICS


# ------------------------------------------------------------------ labels


def test_label_apply_copies_and_label_remove_only_removes_label_occurrence(app, caller, seed):
    mkmailbox(app, "Labels/Urgent")
    (t,) = seed("INBOX", 1, subject="labelled", message_id="<lbl@x>")
    applied = messages.label_apply(app, caller, [t], "Urgent")
    assert applied.status is OperationStatus.SUCCEEDED and applied.kind == "labels.apply"
    labelled = applied.items[0].new_handle
    assert parse(labelled).mailbox == "Labels/Urgent"
    assert len(uids_in(app, "INBOX")) == 1 and len(uids_in(app, "Labels/Urgent")) == 1

    removed = messages.label_remove(app, caller, [labelled])
    assert removed.status is OperationStatus.SUCCEEDED and removed.kind == "labels.remove"
    assert uids_in(app, "Labels/Urgent") == []
    assert len(uids_in(app, "INBOX")) == 1  # the location occurrence remains
    (prior,) = [p for _, p in app.journal.items(removed.operation_id)]
    assert prior["label"] == "Labels/Urgent" and prior["message_id"] == "<lbl@x>"


def test_label_remove_rejects_non_label_occurrence(app, caller, seed):
    (t,) = seed("INBOX", 1)
    with pytest.raises(MailError) as e:
        messages.label_remove(app, caller, [t])
    assert e.value.code is ErrorCode.UNSUPPORTED_SEMANTICS
    assert len(uids_in(app, "INBOX")) == 1


def test_label_apply_unknown_label_not_found(app, caller, seed):
    (t,) = seed("INBOX", 1)
    with pytest.raises(MailError) as e:
        messages.label_apply(app, caller, [t], "Missing")
    assert e.value.code is ErrorCode.NOT_FOUND


# ------------------------------------------------------------------ deletion


def test_mark_and_clear_deleted(app, caller, seed):
    (t,) = seed("INBOX", 1)
    out = messages.mark_deleted(app, caller, [t])
    assert out.status is OperationStatus.SUCCEEDED
    assert "\\Deleted" in flags_of(app, t) and len(uids_in(app, "INBOX")) == 1
    messages.clear_deleted(app, caller, [t])
    assert "\\Deleted" not in flags_of(app, t)


def test_mark_deleted_is_permanent_delete_family(make_app, caller, seed):
    app = make_app(Preset.ASSISTANT)
    (t,) = seed("INBOX", 1)
    out = messages.mark_deleted(app, caller, [t])
    assert out.status is OperationStatus.PENDING
    assert "\\Deleted" not in flags_of(app, t)


def test_expunge_only_from_trash_and_spam(app, caller, seed):
    (t,) = seed("INBOX", 1)
    with pytest.raises(MailError) as e:
        messages.expunge(app, caller, [t])
    assert e.value.code is ErrorCode.UNSUPPORTED_SEMANTICS
    assert len(uids_in(app, "INBOX")) == 1
    mkmailbox(app, "Labels/L")
    (lab,) = seed("Labels/L", 1)
    with pytest.raises(MailError) as e2:
        messages.expunge(app, caller, [lab])
    assert e2.value.code is ErrorCode.UNSUPPORTED_SEMANTICS


def test_expunge_trash_does_not_touch_unrelated_deleted_message(app, caller, seed):
    target, bystander = seed("Trash", 2)
    app.store(ACCOUNT).store_flags("Trash", parse(bystander).uidvalidity,
                                   [parse(bystander).uid], add=["\\Deleted"])
    out = messages.expunge(app, caller, [target])
    assert out.status is OperationStatus.SUCCEEDED and out.kind == "messages.expunge"
    assert uids_in(app, "Trash") == [parse(bystander).uid]
    assert "\\Deleted" in flags_of(app, bystander)


def test_expunge_under_assistant_asks(make_app, caller, owner, seed):
    app = make_app(Preset.ASSISTANT)
    (t,) = seed("Trash", 1)
    out = messages.expunge(app, caller, [t])
    assert out.status is OperationStatus.PENDING and len(uids_in(app, "Trash")) == 1
    app.approve(owner, out.operation_id)
    done = app.resume(caller, out.operation_id)
    assert done.status is OperationStatus.SUCCEEDED and uids_in(app, "Trash") == []


# ------------------------------------------------------------------ policy


def test_reader_denies_writes(make_app, caller, seed):
    app = make_app(Preset.READER)
    (t,) = seed("INBOX", 1)
    for call in (lambda: messages.set_flags(app, caller, [t], add=["read"]),
                 lambda: messages.archive(app, caller, [t]),
                 lambda: messages.trash(app, caller, [t])):
        with pytest.raises(MailError) as e:
            call()
        assert e.value.code is ErrorCode.POLICY_DENIED
    assert len(uids_in(app, "INBOX")) == 1 and flags_of(app, t) == []


def test_destination_deny_rule_blocks_move(make_app, caller, seed):
    app = make_app(Preset.AUTONOMOUS, rules=[
        PolicyRule(family=F.ORGANIZE, action=Action.DENY, mailbox="Folders/Private")])
    mkmailbox(app, "Folders/Private")
    (t,) = seed("INBOX", 1)
    with pytest.raises(MailError) as e:
        messages.move(app, caller, [t], "Folders/Private")
    assert e.value.code is ErrorCode.POLICY_DENIED
    assert len(uids_in(app, "INBOX")) == 1
    messages.archive(app, caller, [t])  # other destinations still work


def test_source_deny_rule_blocks_move(make_app, caller, seed):
    app = make_app(Preset.AUTONOMOUS, rules=[
        PolicyRule(family=F.ORGANIZE, action=Action.DENY, mailbox="INBOX")])
    (t,) = seed("INBOX", 1)
    with pytest.raises(MailError) as e:
        messages.archive(app, caller, [t])
    assert e.value.code is ErrorCode.POLICY_DENIED


def test_max_batch_size_constraint(make_app, caller, seed):
    app = make_app(Preset.AUTONOMOUS, constraints=Constraints(max_batch_size=2))
    t = seed("INBOX", 3)
    with pytest.raises(MailError) as e:
        messages.archive(app, caller, t)
    assert e.value.code is ErrorCode.CONSTRAINT_VIOLATION


def test_ask_then_approve_executes_stored_request(make_app, caller, owner, seed):
    app = make_app(Preset.ASSISTANT)
    (t,) = seed("INBOX", 1)
    out = messages.mark_deleted(app, caller, [t])
    assert out.status is OperationStatus.PENDING
    app.approve(owner, out.operation_id)
    done = app.resume(caller, out.operation_id)
    assert done.status is OperationStatus.SUCCEEDED and "\\Deleted" in flags_of(app, t)


# ------------------------------------------------------------------ execution-time checks


def test_effects_changed_between_request_and_execution_is_conflict(make_app, caller, owner, seed):
    app = make_app(Preset.AUTONOMOUS, rules=[
        PolicyRule(family=F.ORGANIZE, action=Action.ASK, client="hermes")])
    mkmailbox(app, "Folders/Work")
    (t,) = seed("INBOX", 1)
    out = messages.move(app, caller, [t], "Folders/Work")
    assert out.status is OperationStatus.PENDING
    # the destination becomes a different kind of mailbox: delete it and recreate as a label
    store = app.store(ACCOUNT)
    store.delete_mailbox("Folders/Work")
    app.approve(owner, out.operation_id)
    done = app.resume(caller, out.operation_id)
    assert done.status is OperationStatus.FAILED and done.error["code"] == "conflict"
    assert len(uids_in(app, "INBOX")) == 1


# ------------------------------------------------------------------ stale / partial / cancel


def test_stale_handle_fails_item_and_rest_continue(app, caller, seed):
    good = seed("INBOX", 2)
    arch = seed("Archive", 1)
    h = parse(arch[0])
    stale = MessageHandle(account=ACCOUNT, mailbox="Archive", uidvalidity=h.uidvalidity + 9,
                          uid=h.uid).token()
    out = messages.trash(app, caller, [*good, stale])
    assert out.status is OperationStatus.PARTIALLY_SUCCEEDED
    by = {i.target: i for i in out.items}
    assert by[stale].status == "failed" and by[stale].detail == "stale_handle"
    assert all(by[g].status == "succeeded" for g in good)
    assert len(uids_in(app, "Trash")) == 2 and len(uids_in(app, "Archive")) == 1


def test_all_stale_is_failed(app, caller, seed):
    (t,) = seed("INBOX", 1)
    h = parse(t)
    stale = MessageHandle(account=ACCOUNT, mailbox="INBOX", uidvalidity=h.uidvalidity + 9,
                          uid=h.uid).token()
    out = messages.archive(app, caller, [stale])
    assert out.status is OperationStatus.FAILED and out.items[0].detail == "stale_handle"


def test_vanished_message_fails_only_that_item(app, caller, seed):
    t = seed("INBOX", 2)
    h = parse(t[0])
    app.store(ACCOUNT).store_flags("INBOX", h.uidvalidity, [h.uid], add=["\\Deleted"])
    app.store(ACCOUNT).expunge_uids("INBOX", h.uidvalidity, [h.uid])
    out = messages.archive(app, caller, t)
    by = {i.target: i for i in out.items}
    assert out.status is OperationStatus.PARTIALLY_SUCCEEDED
    assert by[t[0]].status == "failed" and by[t[0]].detail == "not_found"
    assert by[t[1]].status == "succeeded"


def test_cancellation_between_chunks_skips_the_rest(app, caller, seed, monkeypatch):
    monkeypatch.setattr(messages, "CHUNK", 2)
    t = seed("INBOX", 5)
    calls = {"n": 0}

    def fake_cancelled(op_id: str) -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # first chunk runs, then cancel is observed

    monkeypatch.setattr(messages, "_cancelled", fake_cancelled)
    out = messages.archive(app, caller, t)
    assert out.status is OperationStatus.PARTIALLY_SUCCEEDED
    statuses = [i.status for i in out.items]
    assert statuses == ["succeeded", "succeeded", "skipped", "skipped", "skipped"]
    assert out.result["cancelled"] is True and len(uids_in(app, "Archive")) == 2


def test_request_cancel_registry_is_cleared_after_execution(app, caller, seed):
    messages.request_cancel("op_other")
    assert messages._cancelled("op_other")
    messages._clear_cancel("op_other")
    assert not messages._cancelled("op_other")
    # a cancel requested for an unrelated id does not affect execution
    messages.request_cancel("op_unrelated")
    try:
        t = seed("INBOX", 1)
        assert messages.archive(app, caller, t).status is OperationStatus.SUCCEEDED
    finally:
        messages._clear_cancel("op_unrelated")


def test_large_batch_is_chunked(app, caller, seed, monkeypatch):
    monkeypatch.setattr(messages, "CHUNK", 3)
    t = seed("INBOX", 8)
    out = messages.archive(app, caller, t)
    assert out.status is OperationStatus.SUCCEEDED and len(out.items) == 8
    assert len(uids_in(app, "Archive")) == 8


# ------------------------------------------------------------------ bulk preview


def test_bulk_preview_by_handles(app, caller, seed):
    mkmailbox(app, "Labels/L")
    a = seed("INBOX", 2, message_id="<same@x>")
    b = seed("Archive", 1, message_id="<same@x>")
    c = seed("INBOX", 1, message_id="<other@x>")
    pv = messages.bulk_preview(app, caller, ACCOUNT, "trash", handles=[*a, *b, *c])
    assert pv["occurrences"] == 4 and pv["truncated"] is False
    assert {m["mailbox"]: m["count"] for m in pv["by_mailbox"]} == {"INBOX": 3, "Archive": 1}
    assert pv["unique_messages"]["count"] == 2
    assert pv["unique_messages"]["certainty"] == "heuristic"
    for m in pv["by_mailbox"]:
        assert m["plan"]["effects"] == ["trash"] and m["plan"]["family"] == "organize"
    assert pv["policy"]["action"] == "allow"
    # nothing changed and nothing was journaled
    assert len(uids_in(app, "INBOX")) == 3 and len(uids_in(app, "Trash")) == 0
    assert app.journal.list() == []


def test_bulk_preview_reports_policy_action_and_unsupported(make_app, caller):
    app = make_app(Preset.ASSISTANT)
    mkmailbox(app, "Labels/L")
    t = put(app, "Trash", 2)
    pv = messages.bulk_preview(app, caller, ACCOUNT, "expunge", handles=t)
    assert pv["policy"]["action"] == "ask"
    assert pv["by_mailbox"][0]["plan"]["effects"] == ["destroy"]
    assert pv["by_mailbox"][0]["plan"]["reversible"] is False
    (lab,) = put(app, "Labels/L", 1)
    pv2 = messages.bulk_preview(app, caller, ACCOUNT, "move", handles=[lab], dest="Archive")
    assert pv2["by_mailbox"][0]["error"]["code"] == "unsupported_semantics"
    assert pv2["policy"] is None


def test_bulk_preview_by_query_and_deny(make_app, caller, seed):
    app = make_app(Preset.READER)
    seed("INBOX", 3, subject="promo deal")
    seed("INBOX", 1, subject="personal")
    pv = messages.bulk_preview(app, caller, ACCOUNT, "archive", query=SearchQuery(subject="promo"))
    assert pv["occurrences"] == 3 and pv["policy"]["action"] == "deny"
    empty = messages.bulk_preview(app, caller, ACCOUNT, "archive",
                                  query=SearchQuery(subject="nothing-matches"))
    assert empty["occurrences"] == 0


def test_bulk_preview_truncates_query_results(app, caller, seed):
    seed("INBOX", 4, subject="promo")
    pv = messages.bulk_preview(app, caller, ACCOUNT, "archive", query=SearchQuery(subject="promo"),
                               limit=3)
    assert pv["occurrences"] == 3 and pv["truncated"] is True


def test_bulk_preview_argument_validation(app, caller, seed):
    (t,) = seed("INBOX", 1)
    with pytest.raises(MailError):
        messages.bulk_preview(app, caller, ACCOUNT, "explode", handles=[t])
    with pytest.raises(MailError):
        messages.bulk_preview(app, caller, ACCOUNT, "archive")
    with pytest.raises(MailError):
        messages.bulk_preview(app, caller, ACCOUNT, "archive", handles=[t],
                              query=SearchQuery())


def test_make_raw_import_is_usable():
    assert b"Subject: Hello" in make_raw()
