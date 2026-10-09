"""Best-effort undo: reversible operations come back; irreversible ones are refused."""

from __future__ import annotations

import pytest
from tests.jobs.conftest import ACCOUNT, outgoing

from mcp_proton.domain.errors import ErrorCode, MailError
from mcp_proton.domain.models import MessageHandle, OperationStatus
from mcp_proton.domain.requests import CallerContext
from mcp_proton.jobs.undo import undo
from mcp_proton.policy.model import Preset
from mcp_proton.services import drafts, mailboxes, messages, sending


def uids(app, mailbox):
    return app.store(ACCOUNT).list_uids(mailbox)[1]


def flags_of(app, handle):
    h = MessageHandle.parse(handle)
    return app.store(ACCOUNT).fetch_flags(h.mailbox, h.uidvalidity, [h.uid])[h.uid]


def test_undo_move(app, caller, seed):
    [h] = seed(subject="Move me")
    out = messages.archive(app, caller, [h])
    assert uids(app, "INBOX") == [] and len(uids(app, "Archive")) == 1
    res = undo(app, caller, out.operation_id)
    assert res["status"] == "succeeded" and res["items"][0]["target"] == h
    assert len(uids(app, "INBOX")) == 1 and uids(app, "Archive") == []
    # a NEW operation went through the normal services
    [op] = res["operations"]
    assert op["operation_id"] != out.operation_id and op["kind"] == "messages.move"
    assert app.journal.get(op["operation_id"]).status is OperationStatus.SUCCEEDED


def test_undo_trash_restores_to_original_folder(app, caller, owner, seed):
    mailboxes.create_folder(app, owner, ACCOUNT, "Projects")
    [h] = seed()
    moved = messages.move(app, caller, [h], "Folders/Projects")
    trashed = messages.trash(app, caller, [moved.items[0].new_handle])
    res = undo(app, caller, trashed.operation_id)
    assert res["status"] == "succeeded"
    assert len(uids(app, "Folders/Projects")) == 1 and uids(app, "Trash") == []


def test_undo_flags_reverts_only_what_changed(app, caller, seed):
    [a, b] = seed(n=2)
    messages.set_flags(app, caller, [b], add=["star"])  # b was already starred before
    out = messages.set_flags(app, caller, [a, b], add=["read", "star"])
    assert "\\Seen" in flags_of(app, a) and "\\Flagged" in flags_of(app, a)
    res = undo(app, caller, out.operation_id)
    assert res["status"] == "succeeded"
    assert "\\Seen" not in flags_of(app, a) and "\\Flagged" not in flags_of(app, a)
    assert "\\Seen" not in flags_of(app, b)
    assert "\\Flagged" in flags_of(app, b)  # it was starred before the operation: kept


def test_undo_removes_flag_the_operation_cleared(app, caller, seed):
    [h] = seed(flags=["\\Seen"])
    out = messages.set_flags(app, caller, [h], remove=["read"])
    assert "\\Seen" not in flags_of(app, h)
    undo(app, caller, out.operation_id)
    assert "\\Seen" in flags_of(app, h)


def test_undo_label_apply(app, caller, owner, seed):
    mailboxes.create_label(app, owner, ACCOUNT, "Work")
    [h] = seed()
    out = messages.label_apply(app, caller, [h], "Work")
    assert len(uids(app, "Labels/Work")) == 1
    res = undo(app, caller, out.operation_id)
    assert res["status"] == "succeeded"
    assert uids(app, "Labels/Work") == [] and len(uids(app, "INBOX")) == 1  # message untouched


def test_undo_twice_is_refused(app, caller, seed):
    [h] = seed()
    out = messages.archive(app, caller, [h])
    undo(app, caller, out.operation_id)
    with pytest.raises(MailError) as e:
        undo(app, caller, out.operation_id)
    assert e.value.code is ErrorCode.CONFLICT


def test_stale_handle_is_a_per_item_failure(app, caller, seed):
    [a, b] = seed(n=2)
    out = messages.archive(app, caller, [a, b])
    gone = out.items[0].new_handle
    messages.trash(app, caller, [gone])  # the user moved one of them again
    res = undo(app, caller, out.operation_id)
    by = {i["target"]: i for i in res["items"]}
    assert res["status"] == "partially_succeeded"
    assert by[a]["status"] == "failed" and by[b]["status"] == "succeeded"
    assert len(uids(app, "INBOX")) == 1


def test_send_cannot_be_undone(app, caller, smtp_server):
    out = sending.send(app, caller, outgoing())
    assert out.status is OperationStatus.SUCCEEDED
    with pytest.raises(MailError) as e:
        undo(app, caller, out.operation_id)
    assert e.value.code is ErrorCode.UNSUPPORTED
    assert "irreversible" in e.value.message and e.value.details["irreversible"] is True


def test_permanent_deletion_and_draft_discard_refused(app, caller, seed):
    [h] = seed()
    trashed = messages.trash(app, caller, [h])
    new = trashed.items[0].new_handle
    gone = messages.expunge(app, caller, [new])
    assert gone.status is OperationStatus.SUCCEEDED
    with pytest.raises(MailError) as e:
        undo(app, caller, gone.operation_id)
    assert "irreversible" in e.value.message

    d = drafts.create_draft(app, caller, outgoing())
    disc = drafts.discard_draft(app, caller, d.result["handle"])
    with pytest.raises(MailError) as e:
        undo(app, caller, disc.operation_id)
    assert "irreversible" in e.value.message


def test_pending_and_foreign_operations(make_app, caller, owner, seed_app=None):
    app = make_app(Preset.ASSISTANT)
    from tests.services.conftest import make_raw

    r = app.store(ACCOUNT).append("INBOX", make_raw())
    h = MessageHandle(account=ACCOUNT, mailbox="INBOX", uidvalidity=r.uidvalidity,
                      uid=r.uid).token()
    moved = messages.archive(app, caller, [h])
    assert moved.status is OperationStatus.SUCCEEDED
    with pytest.raises(MailError) as e:
        undo(app, CallerContext(client_id="intruder"), moved.operation_id)
    assert e.value.code is ErrorCode.NOT_FOUND
    # unknown operation kinds without recorded state are refused too
    with pytest.raises(MailError):
        undo(app, caller, "op_does_not_exist")


def test_undo_goes_through_policy(make_app, caller):
    from mcp_proton.domain.families import OperationFamily
    from mcp_proton.policy.model import Action, PolicyConfig, PolicyRule

    app = make_app(Preset.AUTONOMOUS)
    from tests.services.conftest import make_raw

    r = app.store(ACCOUNT).append("INBOX", make_raw())
    h = MessageHandle(account=ACCOUNT, mailbox="INBOX", uidvalidity=r.uidvalidity,
                      uid=r.uid).token()
    out = messages.archive(app, caller, [h])
    app.set_policy(PolicyConfig(preset=Preset.AUTONOMOUS, rules=[
        PolicyRule(family=OperationFamily.ORGANIZE, action=Action.DENY)]))
    with pytest.raises(MailError) as e:
        undo(app, caller, out.operation_id)
    assert e.value.code is ErrorCode.POLICY_DENIED
    assert uids(app, "INBOX") == []
