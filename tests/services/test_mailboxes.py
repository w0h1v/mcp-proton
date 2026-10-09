"""Mailbox discovery and management through the real IMAP adapter against pymap."""

from __future__ import annotations

import pytest

from mcp_proton.domain.errors import ErrorCode, MailError
from mcp_proton.domain.families import OperationFamily as F
from mcp_proton.domain.models import MailboxRole, MessageHandle, OperationStatus
from mcp_proton.policy.model import Action, Constraints, PolicyRule, Preset
from mcp_proton.services import mailboxes, messages

from .conftest import ACCOUNT, make_raw


def names(app) -> set[str]:
    return {m.name for m in app.store(ACCOUNT).list_mailboxes()}


def put(app, mailbox: str, n: int = 1, **kw) -> list[str]:
    out = []
    for i in range(n):
        r = app.store(ACCOUNT).append(mailbox, make_raw(**{"subject": f"Message {i}", **kw}))
        out.append(MessageHandle(account=ACCOUNT, mailbox=mailbox, uidvalidity=r.uidvalidity,
                                 uid=r.uid).token())
    return out


def uids_in(app, mailbox: str) -> list[int]:
    return app.store(ACCOUNT).list_uids(mailbox)[1]


def ask_then_run(app, caller, owner, outcome):
    assert outcome.status is OperationStatus.PENDING
    app.approve(owner, outcome.operation_id)
    return app.resume(caller, outcome.operation_id)


# ------------------------------------------------------------------ reads


def test_list_mailboxes_roles_and_counts(app, caller, seed):
    seed("INBOX", 2)
    infos = {m.name: m for m in mailboxes.list_mailboxes(app, caller, ACCOUNT, with_counts=True)}
    assert infos["INBOX"].role is MailboxRole.INBOX and infos["INBOX"].messages == 2
    assert infos["Trash"].role is MailboxRole.TRASH and infos["Archive"].role is MailboxRole.ARCHIVE
    assert infos["Folders"].role is MailboxRole.CONTAINER
    plain = mailboxes.list_mailboxes(app, caller, ACCOUNT)
    assert all(m.messages is None for m in plain)


def test_status_reports_counts_and_uidvalidity(app, caller, seed):
    seed("INBOX", 3)
    st = mailboxes.status(app, caller, ACCOUNT, "INBOX")
    assert st.messages == 3 and st.uidvalidity and st.uidnext and st.unseen == 3


def test_list_hides_mailboxes_outside_constraints(make_app, caller):
    app = make_app(Preset.AUTONOMOUS, constraints=Constraints(
        allowed_mailboxes={ACCOUNT: ["INBOX", "Archive"]}))
    assert {m.name for m in mailboxes.list_mailboxes(app, caller, ACCOUNT)} == {"INBOX", "Archive"}


def test_list_hides_mailboxes_with_deny_rule(make_app, caller):
    app = make_app(Preset.AUTONOMOUS, rules=[
        PolicyRule(family=F.READ, action=Action.DENY, mailbox="Trash")])
    listed = {m.name for m in mailboxes.list_mailboxes(app, caller, ACCOUNT)}
    assert "Trash" not in listed and "INBOX" in listed
    with pytest.raises(MailError):
        mailboxes.status(app, caller, ACCOUNT, "Trash")


# ------------------------------------------------------------------ create


def test_create_folder_and_nested_folder(app, caller):
    out = mailboxes.create_folder(app, caller, ACCOUNT, "Work")
    assert out.status is OperationStatus.SUCCEEDED and out.result == {"mailbox": "Folders/Work"}
    assert "Folders/Work" in names(app)
    nested = mailboxes.create_folder(app, caller, ACCOUNT, "Projects", parent="Work")
    assert nested.status is OperationStatus.SUCCEEDED
    assert "Folders/Work/Projects" in names(app)
    also = mailboxes.create_folder(app, caller, ACCOUNT, "Work/Deep")
    assert also.status is OperationStatus.SUCCEEDED and "Folders/Work/Deep" in names(app)
    prefixed = mailboxes.create_folder(app, caller, ACCOUNT, "Other", parent="Folders/Work")
    assert prefixed.result == {"mailbox": "Folders/Work/Other"}


def test_create_folder_requires_existing_parent_and_new_name(app, caller):
    with pytest.raises(MailError) as e:
        mailboxes.create_folder(app, caller, ACCOUNT, "Child", parent="Missing")
    assert e.value.code is ErrorCode.NOT_FOUND
    mailboxes.create_folder(app, caller, ACCOUNT, "Dup")
    with pytest.raises(MailError) as e2:
        mailboxes.create_folder(app, caller, ACCOUNT, "Dup")
    assert e2.value.code is ErrorCode.CONFLICT


@pytest.mark.parametrize("bad", ["", " pad", "a//b", "w*ld", "x%y", "..", "a/../b"])
def test_create_folder_rejects_bad_names(app, caller, bad):
    with pytest.raises(MailError) as e:
        mailboxes.create_folder(app, caller, ACCOUNT, bad)
    assert e.value.code is ErrorCode.INVALID_REQUEST


def test_create_label(app, caller):
    out = mailboxes.create_label(app, caller, ACCOUNT, "Receipts")
    assert out.status is OperationStatus.SUCCEEDED and "Labels/Receipts" in names(app)
    with pytest.raises(MailError) as e:
        mailboxes.create_label(app, caller, ACCOUNT, "A/B")
    assert e.value.code is ErrorCode.INVALID_REQUEST


def test_create_is_organize_and_reader_is_denied(make_app, caller):
    app = make_app(Preset.READER)
    with pytest.raises(MailError) as e:
        mailboxes.create_folder(app, caller, ACCOUNT, "Nope")
    assert e.value.code is ErrorCode.POLICY_DENIED
    assert "Folders/Nope" not in names(app)


def test_create_respects_mailbox_deny_rule(make_app, caller):
    app = make_app(Preset.AUTONOMOUS, rules=[
        PolicyRule(family=F.ORGANIZE, action=Action.DENY, mailbox="Folders/Secret")])
    with pytest.raises(MailError) as e:
        mailboxes.create_folder(app, caller, ACCOUNT, "Secret")
    assert e.value.code is ErrorCode.POLICY_DENIED


# ------------------------------------------------------------------ rename


def test_rename_folder_relative_and_full(app, caller):
    mailboxes.create_folder(app, caller, ACCOUNT, "Old")
    out = mailboxes.rename(app, caller, ACCOUNT, "Folders/Old", "New")
    assert out.status is OperationStatus.SUCCEEDED
    assert out.result == {"mailbox": "Folders/New", "previous": "Folders/Old"}
    assert "Folders/New" in names(app) and "Folders/Old" not in names(app)
    mailboxes.rename(app, caller, ACCOUNT, "Folders/New", "Folders/Newer")
    assert "Folders/Newer" in names(app)


def test_rename_label_and_cross_namespace_refused(app, caller):
    mailboxes.create_label(app, caller, ACCOUNT, "Tag")
    mailboxes.create_folder(app, caller, ACCOUNT, "Dir")
    out = mailboxes.rename(app, caller, ACCOUNT, "Labels/Tag", "Tag2")
    assert out.status is OperationStatus.SUCCEEDED and "Labels/Tag2" in names(app)
    with pytest.raises(MailError) as e:
        mailboxes.rename(app, caller, ACCOUNT, "Labels/Tag2", "Folders/Tag2")
    assert e.value.code is ErrorCode.INVALID_REQUEST
    with pytest.raises(MailError) as e2:
        mailboxes.rename(app, caller, ACCOUNT, "Folders/Dir", "Labels/Dir")
    assert e2.value.code is ErrorCode.INVALID_REQUEST


def test_rename_system_mailbox_refused(app, caller):
    for system in ("Archive", "INBOX", "Trash", "Folders", "Labels"):
        with pytest.raises(MailError) as e:
            mailboxes.rename(app, caller, ACCOUNT, system, "Renamed")
        assert e.value.code is ErrorCode.UNSUPPORTED_SEMANTICS
    assert "Archive" in names(app)


def test_rename_to_existing_or_missing(app, caller):
    mailboxes.create_folder(app, caller, ACCOUNT, "A")
    mailboxes.create_folder(app, caller, ACCOUNT, "B")
    with pytest.raises(MailError) as e:
        mailboxes.rename(app, caller, ACCOUNT, "Folders/A", "B")
    assert e.value.code is ErrorCode.CONFLICT
    with pytest.raises(MailError) as e2:
        mailboxes.rename(app, caller, ACCOUNT, "Folders/Nope", "C")
    assert e2.value.code is ErrorCode.NOT_FOUND


def test_rename_scope_includes_destination_for_policy(make_app, caller):
    app = make_app(Preset.AUTONOMOUS, rules=[
        PolicyRule(family=F.ORGANIZE, action=Action.DENY, mailbox="Folders/Locked")])
    app.store(ACCOUNT).create_mailbox("Folders/Src")
    with pytest.raises(MailError) as e:
        mailboxes.rename(app, caller, ACCOUNT, "Folders/Src", "Locked")
    assert e.value.code is ErrorCode.POLICY_DENIED


# ------------------------------------------------------------------ delete


def test_delete_empty_folder_and_label(app, caller):
    mailboxes.create_folder(app, caller, ACCOUNT, "Tmp")
    mailboxes.create_label(app, caller, ACCOUNT, "TmpL")
    out = mailboxes.delete_folder(app, caller, ACCOUNT, "Folders/Tmp")
    assert out.status is OperationStatus.SUCCEEDED and out.kind == "mailboxes.delete_folder"
    out2 = mailboxes.delete_label(app, caller, ACCOUNT, "Labels/TmpL")
    assert out2.status is OperationStatus.SUCCEEDED and out2.kind == "mailboxes.delete_label"
    assert not ({"Folders/Tmp", "Labels/TmpL"} & names(app))


def test_delete_system_folders_refused(app, caller):
    for system in ("Archive", "Trash", "INBOX", "Folders"):
        with pytest.raises(MailError) as e:
            mailboxes.delete_folder(app, caller, ACCOUNT, system)
        assert e.value.code is ErrorCode.UNSUPPORTED_SEMANTICS
    with pytest.raises(MailError):
        mailboxes.delete_label(app, caller, ACCOUNT, "Archive")
    assert "Archive" in names(app)


def test_delete_non_empty_folder_needs_permanent_delete(make_app, caller):
    from mcp_proton.policy.model import Preset as P

    app = make_app(P.CUSTOM, custom_baseline={
        F.READ: Action.ALLOW, F.ORGANIZE: Action.ALLOW, F.FOLDER_DELETE: Action.ALLOW,
        F.PERMANENT_DELETE: Action.DENY})
    mailboxes.create_folder(app, caller, ACCOUNT, "Full")
    mailboxes.create_folder(app, caller, ACCOUNT, "Empty")
    put(app, "Folders/Full", 1)
    with pytest.raises(MailError) as e:
        mailboxes.delete_folder(app, caller, ACCOUNT, "Folders/Full")
    assert e.value.code is ErrorCode.POLICY_DENIED and "Folders/Full" in names(app)
    # the same policy lets an empty folder go
    ok = mailboxes.delete_folder(app, caller, ACCOUNT, "Folders/Empty")
    assert ok.status is OperationStatus.SUCCEEDED and "Folders/Empty" not in names(app)


def test_delete_non_empty_folder_under_assistant_asks(make_app, caller, owner):
    app = make_app(Preset.ASSISTANT)
    mailboxes.create_folder(app, caller, ACCOUNT, "Full")
    put(app, "Folders/Full", 2)
    out = mailboxes.delete_folder(app, caller, ACCOUNT, "Folders/Full")
    assert out.status is OperationStatus.PENDING
    req = app.journal.get(out.operation_id).request
    assert req.payload["_also_families"] == ["permanent_delete"]
    done = ask_then_run(app, caller, owner, out)
    assert done.status is OperationStatus.SUCCEEDED and "Folders/Full" not in names(app)


def test_delete_folder_that_became_non_empty_conflicts(make_app, caller, owner):
    app = make_app(Preset.ASSISTANT)
    mailboxes.create_folder(app, caller, ACCOUNT, "Racy")
    out = mailboxes.delete_folder(app, caller, ACCOUNT, "Folders/Racy")  # planned as empty
    assert out.status is OperationStatus.PENDING
    assert app.journal.get(out.operation_id).request.payload["_also_families"] == []
    put(app, "Folders/Racy", 1)  # mail arrives before the owner approves
    done = ask_then_run(app, caller, owner, out)
    assert done.status is OperationStatus.FAILED and done.error["code"] == "conflict"
    assert "Folders/Racy" in names(app) and len(uids_in(app, "Folders/Racy")) == 1


def test_delete_folder_with_subfolders_refused(app, caller):
    mailboxes.create_folder(app, caller, ACCOUNT, "Parent")
    mailboxes.create_folder(app, caller, ACCOUNT, "Kid", parent="Parent")
    with pytest.raises(MailError) as e:
        mailboxes.delete_folder(app, caller, ACCOUNT, "Folders/Parent")
    assert e.value.code is ErrorCode.CONFLICT


def test_delete_label_leaves_message_locations(make_app, caller, owner):
    app = make_app(Preset.ASSISTANT)
    mailboxes.create_label(app, caller, ACCOUNT, "Gone")
    (t,) = put(app, "INBOX", 1)
    messages.label_apply(app, caller, [t], "Gone")
    out = mailboxes.delete_label(app, caller, ACCOUNT, "Labels/Gone")
    assert out.status is OperationStatus.PENDING  # label_delete is Ask under Assistant
    done = ask_then_run(app, caller, owner, out)
    assert done.status is OperationStatus.SUCCEEDED and len(uids_in(app, "INBOX")) == 1


# ------------------------------------------------------------------ subscribe


def test_subscribe_and_unsubscribe(app, caller):
    mailboxes.create_folder(app, caller, ACCOUNT, "Sub")
    off = mailboxes.subscribe(app, caller, ACCOUNT, "Folders/Sub", False)
    assert off.status is OperationStatus.SUCCEEDED and off.kind == "mailboxes.subscribe"
    state = {m.name: m.subscribed for m in app.store(ACCOUNT).list_mailboxes()}
    assert state["Folders/Sub"] is False
    mailboxes.subscribe(app, caller, ACCOUNT, "Folders/Sub", True)
    state = {m.name: m.subscribed for m in app.store(ACCOUNT).list_mailboxes()}
    assert state["Folders/Sub"] is True
    with pytest.raises(MailError) as e:
        mailboxes.subscribe(app, caller, ACCOUNT, "Nope", True)
    assert e.value.code is ErrorCode.NOT_FOUND


# ------------------------------------------------------------------ empty


def test_empty_trash_removes_snapshot(app, caller, seed):
    seed("Trash", 3)
    out = mailboxes.empty(app, caller, ACCOUNT, "Trash")
    assert out.status is OperationStatus.SUCCEEDED and out.kind == "mailboxes.empty"
    assert out.result["expunged"] == 3 and out.result["snapshot_size"] == 3
    assert uids_in(app, "Trash") == []


def test_empty_spam_and_refuses_other_mailboxes(app, caller, seed):
    seed("Spam", 2)
    assert mailboxes.empty(app, caller, ACCOUNT, "Spam").status is OperationStatus.SUCCEEDED
    mailboxes.create_folder(app, caller, ACCOUNT, "F")
    mailboxes.create_label(app, caller, ACCOUNT, "L")
    seed("INBOX", 1)
    for mb in ("INBOX", "Archive", "Folders/F", "Labels/L", "Sent"):
        with pytest.raises(MailError) as e:
            mailboxes.empty(app, caller, ACCOUNT, mb)
        assert e.value.code is ErrorCode.UNSUPPORTED_SEMANTICS
    assert len(uids_in(app, "INBOX")) == 1


def test_empty_snapshot_spares_late_arrivals(make_app, caller, owner):
    app = make_app(Preset.ASSISTANT)
    put(app, "Trash", 2)
    out = mailboxes.empty(app, caller, ACCOUNT, "Trash")
    assert out.status is OperationStatus.PENDING
    late = put(app, "Trash", 1, subject="arrived later")[0]
    done = ask_then_run(app, caller, owner, out)
    assert done.status is OperationStatus.SUCCEEDED and done.result["expunged"] == 2
    assert uids_in(app, "Trash") == [MessageHandle.parse(late).uid]


def test_empty_leaves_unrelated_deleted_flag_alone(app, caller, seed):
    (keep,) = seed("INBOX", 1)  # not in Trash: untouched regardless
    seed("Trash", 1)
    h = MessageHandle.parse(keep)
    app.store(ACCOUNT).store_flags("INBOX", h.uidvalidity, [h.uid], add=["\\Deleted"])
    mailboxes.empty(app, caller, ACCOUNT, "Trash")
    assert uids_in(app, "INBOX") == [h.uid]


def test_empty_under_assistant_asks_and_records_snapshot_in_payload(make_app, caller):
    app = make_app(Preset.ASSISTANT)
    put(app, "Trash", 4)
    out = mailboxes.empty(app, caller, ACCOUNT, "Trash")
    assert out.status is OperationStatus.PENDING
    req = app.journal.get(out.operation_id).request
    assert req.payload["snapshot_size"] == 4 and req.batch_size == 4
    assert "Permanently delete 4 messages from Trash" in req.summary
    assert len(uids_in(app, "Trash")) == 4


def test_empty_cancellation_between_chunks(app, caller, seed, monkeypatch):
    monkeypatch.setattr(messages, "CHUNK", 2)
    seed("Trash", 5)
    calls = {"n": 0}

    def fake(op_id: str) -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(mailboxes, "_cancelled", fake)
    out = mailboxes.empty(app, caller, ACCOUNT, "Trash")
    assert out.status is OperationStatus.PARTIALLY_SUCCEEDED
    assert out.result["expunged"] == 2 and out.result["skipped"] == 3
    assert len(uids_in(app, "Trash")) == 3


def test_empty_with_stale_uidvalidity_fails_without_deleting(make_app, caller, owner):
    app = make_app(Preset.ASSISTANT)
    put(app, "Trash", 1)
    first = mailboxes.empty(app, caller, ACCOUNT, "Trash")
    forged = app.journal.get(first.operation_id).request.model_copy(deep=True)
    forged.payload["uidvalidity"] += 1
    out = app.run(caller, forged)
    done = ask_then_run(app, caller, owner, out)
    assert done.status is OperationStatus.FAILED and done.error["code"] == "stale_handle"
    assert len(uids_in(app, "Trash")) == 1


def test_uid_set_roundtrip():
    enc = mailboxes._encode_uids([5, 1, 2, 3, 9, 10, 20])
    assert enc == "1:3,5,9:10,20"
    assert mailboxes._decode_uids(enc) == [1, 2, 3, 5, 9, 10, 20]
    assert mailboxes._encode_uids([]) == "" and mailboxes._decode_uids("") == []
