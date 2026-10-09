"""Reminders (event emission) and snooze (file now, restore later)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.jobs.conftest import ACCOUNT

from mcp_proton.domain.errors import MailError
from mcp_proton.domain.families import OperationFamily
from mcp_proton.domain.models import MessageHandle, OperationStatus
from mcp_proton.jobs import common, reminders
from mcp_proton.jobs.store import JobStore
from mcp_proton.policy.model import Action, PolicyRule, Preset
from mcp_proton.services import events, mailboxes


def uids(app, mailbox):
    return app.store(ACCOUNT).list_uids(mailbox)[1]


def runs(app, job_id):
    return JobStore(app.db).runs(job_id)


# ------------------------------------------------------------------ reminders


def test_reminder_emits_event_at_due_time(app, caller, seed, clock, make_scheduler):
    [h] = seed(subject="Call the bank")
    due = clock.after(hours=2)
    out = reminders.remind(app, caller, h, due, "ring them back")
    assert out.status is OperationStatus.SUCCEEDED
    job = JobStore(app.db).get(out.result["job_id"])
    assert "not a Proton feature" in job.public()["note"]

    sched = make_scheduler(app)
    sched.tick(due - timedelta(seconds=1))
    assert events.list_events(app, caller, ACCOUNT).events == []

    sched.tick(due + timedelta(seconds=1))
    [ev] = events.list_events(app, caller, ACCOUNT).events
    assert ev["type"] == "reminder_due" and ev["mailbox"] == "INBOX"
    assert ev["data"]["handle"] == h and ev["data"]["note"] == "ring them back"
    assert ev["data"]["message_exists"] is True
    assert "subject" not in ev["data"]

    sched.tick(due + timedelta(days=1))  # one-shot
    assert len(events.list_events(app, caller, ACCOUNT).events) == 1


def test_reminder_can_star_the_message(app, caller, seed, clock, make_scheduler):
    [h] = seed()
    due = clock.after(hours=1)
    reminders.remind(app, caller, h, due, star=True)
    make_scheduler(app).tick(due + timedelta(seconds=1))
    parsed = MessageHandle.parse(h)
    flags = app.store(ACCOUNT).fetch_flags("INBOX", parsed.uidvalidity, [parsed.uid])
    assert "\\Flagged" in flags[parsed.uid]


def test_reminder_validation(app, caller, seed, clock):
    [h] = seed()
    with pytest.raises(MailError):
        reminders.remind(app, caller, h, clock.after(hours=-1))
    with pytest.raises(MailError):
        reminders.remind(app, caller, h, clock.after(hours=1), "x" * 501)


# ------------------------------------------------------------------ snooze


def test_snooze_moves_now_and_restores_later(app, caller, seed, clock, make_scheduler):
    [h] = seed(subject="Later please")
    until = clock.after(hours=3)
    out = reminders.snooze(app, caller, [h], until, "Archive")
    assert out.status is OperationStatus.SUCCEEDED
    assert out.result["filing"]["status"] == "succeeded"
    assert uids(app, "INBOX") == [] and len(uids(app, "Archive")) == 1  # filed immediately

    job_id = out.result["job_id"]
    job = JobStore(app.db).get(job_id)
    assert job.spec["phase"] == "restore" and job.next_run_at == until
    assert "not Proton's native snooze" in job.public()["note"]

    sched = make_scheduler(app)
    sched.tick(until - timedelta(minutes=1))
    assert len(uids(app, "Archive")) == 1  # still snoozed

    sched.tick(until + timedelta(seconds=1))
    assert len(uids(app, "INBOX")) == 1 and uids(app, "Archive") == []
    assert JobStore(app.db).get(job_id).status == "done"
    assert [r.status for r in runs(app, job_id)] == ["succeeded", "succeeded"]


def test_snooze_into_user_folder_restores_to_original_folder(app, caller, owner, seed, clock,
                                                              make_scheduler):
    assert mailboxes.create_folder(app, owner, ACCOUNT, "Later").status is OperationStatus.SUCCEEDED
    [a, b] = seed(n=2)
    until = clock.after(hours=1)
    out = reminders.snooze(app, caller, [a, b], until, "Folders/Later")
    assert out.result["filing"]["status"] == "succeeded"
    assert len(uids(app, "Folders/Later")) == 2
    make_scheduler(app).tick(until + timedelta(seconds=1))
    assert len(uids(app, "INBOX")) == 2 and uids(app, "Folders/Later") == []


def test_snooze_restore_reports_stale_handles(app, caller, seed, clock, make_scheduler):
    from mcp_proton.services import messages

    [h] = seed()
    until = clock.after(hours=1)
    out = reminders.snooze(app, caller, [h], until, "Archive")
    # the user moves the snoozed message elsewhere meanwhile
    snoozed = JobStore(app.db).get(out.result["job_id"]).spec["items"][0]["handle"]
    messages.trash(app, caller, [snoozed])
    make_scheduler(app).tick(until + timedelta(seconds=1))
    [_file, restore] = sorted(runs(app, out.result["job_id"]), key=lambda r: r.id)
    assert restore.status == "failed" and restore.detail["failed"] == 1


def test_snooze_assistant_flow(make_app, caller, owner, clock, make_scheduler):
    app = make_app(Preset.ASSISTANT)
    h = _seed(app, "INBOX")
    until = clock.after(hours=1)
    out = reminders.snooze(app, caller, [h], until, "Archive")
    assert out.status is OperationStatus.PENDING
    assert uids(app, "Archive") == []  # registration is AUTOMATION=Ask: nothing moved yet

    app.approve(owner, out.operation_id, True, execute=True)
    [job] = JobStore(app.db).find()
    assert job.spec["phase"] == "file"
    make_scheduler(app).tick(clock.after(seconds=5))  # the job files the message itself
    assert len(uids(app, "Archive")) == 1


def test_snooze_is_cancelled_when_filing_needs_approval(make_app, caller, clock):
    app = make_app(Preset.AUTONOMOUS, rules=[
        PolicyRule(family=OperationFamily.ORGANIZE, action=Action.ASK)])
    h = _seed(app, "INBOX")
    out = reminders.snooze(app, caller, [h], clock.after(hours=1), "Archive")
    assert out.status is OperationStatus.SUCCEEDED  # AUTOMATION is allowed
    assert out.result["filing"]["status"] == "blocked"
    assert len(uids(app, "INBOX")) == 1 and uids(app, "Archive") == []
    job = JobStore(app.db).get(out.result["job_id"])
    assert job.status == "failed"
    pending = [o for o in app.journal.list(status=OperationStatus.PENDING)
               if o.kind == "messages.move"]
    assert pending == []  # the half-applied move was cancelled, not left for approval


def test_snooze_validates_folder(app, caller, seed, clock):
    [h] = seed()
    with pytest.raises(MailError):
        reminders.snooze(app, caller, [h], clock.after(hours=1), "Trash")
    with pytest.raises(MailError):
        reminders.snooze(app, caller, [h], clock.after(hours=1), "Folders/Nope")


def test_snooze_cancel_leaves_messages(app, caller, seed, clock):
    [h] = seed()
    out = reminders.snooze(app, caller, [h], clock.after(hours=1), "Archive")
    res = common.cancel_job(app, caller, out.result["job_id"])
    assert "does not move" in res.result["note"]
    assert len(uids(app, "Archive")) == 1


def _seed(app, mailbox):
    from tests.services.conftest import make_raw

    r = app.store(ACCOUNT).append(mailbox, make_raw(), flags=None)
    return MessageHandle(account=ACCOUNT, mailbox=mailbox, uidvalidity=r.uidvalidity,
                         uid=r.uid).token()
