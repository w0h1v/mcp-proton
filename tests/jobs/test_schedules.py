"""Local send schedules: firing, restarts, missed runs, crash safety, immutability, policy."""

from __future__ import annotations

import time
from datetime import UTC, timedelta

import pytest
from tests.jobs.conftest import ACCOUNT, delivered_subjects, outgoing

from mcp_proton.domain.errors import ErrorCode, MailError
from mcp_proton.domain.families import OperationFamily
from mcp_proton.domain.models import LocalAttachment, OperationStatus
from mcp_proton.jobs import common, schedules
from mcp_proton.jobs.store import JobStore
from mcp_proton.policy.model import Action, ClientConfig, PolicyConfig, PolicyRule, Preset
from mcp_proton.services import drafts


def schedule_in(app, caller, clock, hours=1.0, **kw):
    out = schedules.schedule_send(app, caller, message=kw.pop("message", outgoing()),
                                  at=clock.after(hours=hours), **kw)
    assert out.status is OperationStatus.SUCCEEDED, out
    return out.result["job_id"], clock.after(hours=hours)


def runs(app, job_id):
    return JobStore(app.db).runs(job_id)


def test_fires_once_at_due_time(app, caller, smtp_server, clock, make_scheduler):
    job_id, due = schedule_in(app, caller, clock)
    sched = make_scheduler(app)
    job = JobStore(app.db).get(job_id)
    assert job.client_id == caller.client_id and job.status == "active"
    assert "not Proton's native scheduled send" in job.public()["note"]

    assert sched.tick(due - timedelta(seconds=1)).fired == 0
    assert smtp_server.messages == []

    rep = sched.tick(due + timedelta(seconds=1))
    assert rep.fired == 1
    assert delivered_subjects(smtp_server) == ["Scheduled hello"]
    assert sched.tick(due + timedelta(hours=5)).fired == 0  # one-shot: never again
    assert len(smtp_server.messages) == 1

    job = JobStore(app.db).get(job_id)
    assert job.status == "done" and job.next_run_at is None
    [run] = runs(app, job_id)
    assert run.status == "succeeded" and run.operation_id
    assert app.journal.get(run.operation_id).status is OperationStatus.SUCCEEDED


def test_job_view_hides_body_but_shows_fingerprint(app, caller, clock):
    job_id, _ = schedule_in(app, caller, clock, message=outgoing(subject="Secret plans"))
    view = common.get_job(app, caller, job_id)
    send = view["spec"]["send"]
    assert send["to"] == ["bob@example.com"] and send["subject"] == "Secret plans"
    assert "body text" not in str(view) and len(send["content_sha256"]) == 64


def test_restart_before_due_still_fires(app, caller, smtp_server, clock, make_scheduler):
    job_id, due = schedule_in(app, caller, clock)
    first = make_scheduler(app)
    first.start()
    first.stop()
    assert not first.running
    second = make_scheduler(app)  # a new process over the same database
    assert second.tick(due + timedelta(seconds=5)).fired == 1
    assert len(smtp_server.messages) == 1


def test_background_thread_fires(app, caller, smtp_server, clock, make_scheduler):
    _, due = schedule_in(app, caller, clock)
    sched = make_scheduler(app, poll_interval=0.02)
    sched.start()
    try:
        clock.now = due + timedelta(seconds=1)
        deadline = time.time() + 10
        while not smtp_server.messages and time.time() < deadline:
            time.sleep(0.02)
    finally:
        sched.stop()
    assert len(smtp_server.messages) == 1


def test_concurrent_schedulers_claim_once(app, caller, smtp_server, clock, make_scheduler):
    _, due = schedule_in(app, caller, clock)
    a, b = make_scheduler(app), make_scheduler(app)
    job = JobStore(app.db).due(due + timedelta(seconds=1))[0]
    assert a.store.claim(job, due + timedelta(minutes=5), due, due) is not None
    assert b.store.claim(job, due + timedelta(minutes=5), due, due) is None  # CAS lost


# ------------------------------------------------------------------ missed-run policy


def test_missed_run_once_sends_late(app, caller, smtp_server, clock, make_scheduler):
    job_id, due = schedule_in(app, caller, clock, missed_run_policy="run_once")
    make_scheduler(app).tick(due + timedelta(days=2))
    assert len(smtp_server.messages) == 1
    assert runs(app, job_id)[0].detail["late"] is True


def test_missed_skip_does_not_send(app, caller, smtp_server, clock, make_scheduler):
    job_id, due = schedule_in(app, caller, clock, missed_run_policy="skip")
    rep = make_scheduler(app).tick(due + timedelta(days=2))
    assert rep.skipped == 1 and smtp_server.messages == []
    [run] = runs(app, job_id)
    assert run.status == "skipped"
    assert JobStore(app.db).get(job_id).status == "done"


def daily_job(app, caller, clock, policy):
    rec = {"freq": "daily", "time": "09:00", "timezone": "Europe/Berlin"}
    out = schedules.schedule_send(app, caller, message=outgoing(subject="Daily"),
                                  recurrence=rec, missed_run_policy=policy)
    assert out.status is OperationStatus.SUCCEEDED
    job = JobStore(app.db).get(out.result["job_id"])
    return job


def drain(sched, now, limit=20):
    total = 0
    for _ in range(limit):
        rep = sched.tick(now)
        if not (rep.fired or rep.skipped):
            break
        total += rep.fired
    return total


def test_recurring_run_once_after_downtime_sends_one_and_resumes(
        app, caller, smtp_server, clock, make_scheduler):
    job = daily_job(app, caller, clock, "run_once")
    later = job.next_run_at + timedelta(days=3, hours=1)
    sched = make_scheduler(app)
    assert drain(sched, later) == 1
    after = JobStore(app.db).get(job.id)
    assert after.status == "active" and after.next_run_at > later
    assert len(smtp_server.messages) == 1


def test_recurring_skip_after_downtime_sends_nothing(app, caller, smtp_server, clock,
                                                     make_scheduler):
    job = daily_job(app, caller, clock, "skip")
    later = job.next_run_at + timedelta(days=3, hours=1)
    sched = make_scheduler(app)
    sched.tick(later)
    assert smtp_server.messages == []
    after = JobStore(app.db).get(job.id)
    assert after.status == "active" and after.next_run_at > later


def test_recurring_run_all_is_capped(app, caller, smtp_server, clock, make_scheduler):
    job = daily_job(app, caller, clock, "run_all")
    # five daily occurrences are due; the cap keeps only the latest three
    later = job.next_run_at + timedelta(days=4, hours=1)
    sched = make_scheduler(app, run_all_cap=3)
    assert drain(sched, later) == 3
    assert len(smtp_server.messages) == 3
    statuses = [r.status for r in runs(app, job.id)]
    assert statuses.count("skipped") == 1 and statuses.count("succeeded") == 3


def test_recurring_run_all_under_cap_runs_every_miss(app, caller, smtp_server, clock,
                                                      make_scheduler):
    job = daily_job(app, caller, clock, "run_all")
    later = job.next_run_at + timedelta(days=2, hours=1)
    assert drain(make_scheduler(app), later) == 3
    assert len(smtp_server.messages) == 3


# ------------------------------------------------------------------ crash safety


def test_crash_after_send_before_run_recorded_never_double_sends(
        app, caller, smtp_server, clock, make_scheduler, monkeypatch):
    job_id, due = schedule_in(app, caller, clock)
    sched = make_scheduler(app, lease_seconds=60)
    real_finish = JobStore.finish_run

    def crash(self, *a, **k):
        raise RuntimeError("process died")

    monkeypatch.setattr(JobStore, "finish_run", crash)
    sched.tick(due + timedelta(seconds=1))  # SMTP accepted, then the "crash"
    assert len(smtp_server.messages) == 1
    [open_run] = runs(app, job_id)
    assert open_run.status == "running"

    monkeypatch.setattr(JobStore, "finish_run", real_finish)
    restarted = make_scheduler(app, lease_seconds=60)
    assert restarted.tick(due + timedelta(seconds=30)).fired == 0  # lease still held
    restarted.tick(due + timedelta(seconds=120))  # lease expired: the open run is resumed
    assert len(smtp_server.messages) == 1  # idempotency: no second send
    [run] = runs(app, job_id)
    assert run.status == "succeeded" and run.detail["resumed"] is True
    assert JobStore(app.db).get(job_id).status == "done"


# ------------------------------------------------------------------ immutability


def make_draft(app, caller):
    out = drafts.create_draft(app, caller, outgoing(subject="Draft subject"))
    assert out.status is OperationStatus.SUCCEEDED
    return out.result["handle"]


def test_draft_schedule_sends_unchanged_draft(app, caller, smtp_server, clock, make_scheduler):
    handle = make_draft(app, caller)
    out = schedules.schedule_send(app, caller, draft=handle, at=clock.after(hours=1))
    assert out.status is OperationStatus.SUCCEEDED
    make_scheduler(app).tick(clock.after(hours=1, seconds=1))
    assert delivered_subjects(smtp_server) == ["Draft subject"]


def test_changed_draft_fails_run_and_sends_nothing(app, caller, smtp_server, clock,
                                                   make_scheduler):
    handle = make_draft(app, caller)
    out = schedules.schedule_send(app, caller, draft=handle, at=clock.after(hours=1))
    job_id = out.result["job_id"]
    edit = drafts.update_draft(app, caller, handle, {"subject": "Edited after scheduling"})
    assert edit.status in (OperationStatus.SUCCEEDED, OperationStatus.PARTIALLY_SUCCEEDED)

    make_scheduler(app).tick(clock.after(hours=1, seconds=1))
    assert smtp_server.messages == []
    [run] = runs(app, job_id)
    assert run.status == "failed" and "nothing was sent" in run.detail["message"]
    assert JobStore(app.db).get(job_id).status == "failed"


def test_deleted_draft_fails_run(app, caller, smtp_server, clock, make_scheduler):
    handle = make_draft(app, caller)
    job_id = schedules.schedule_send(app, caller, draft=handle,
                                     at=clock.after(hours=1)).result["job_id"]
    drafts.discard_draft(app, caller, handle)
    make_scheduler(app).tick(clock.after(hours=1, seconds=1))
    assert smtp_server.messages == []
    assert runs(app, job_id)[0].detail["reason"] == "draft_missing"


def test_changed_attachment_fails_run(app, caller, smtp_server, clock, make_scheduler, tmp_path):
    f = tmp_path / "ingest" / "report.txt"
    f.write_text("v1")
    msg = outgoing(attachments=[LocalAttachment(path=str(f))])
    job_id = schedules.schedule_send(app, caller, message=msg,
                                     at=clock.after(hours=1)).result["job_id"]
    spec = JobStore(app.db).get(job_id).spec
    assert spec["send"]["attachment_manifest"][0]["sha256"]
    f.write_text("v2 changed")
    make_scheduler(app).tick(clock.after(hours=1, seconds=1))
    assert smtp_server.messages == []
    [run] = runs(app, job_id)
    assert run.status == "failed" and "changed since scheduling" in run.detail["message"]


def test_attachment_unchanged_is_sent(app, caller, smtp_server, clock, make_scheduler, tmp_path):
    f = tmp_path / "ingest" / "report.txt"
    f.write_text("v1")
    msg = outgoing(attachments=[LocalAttachment(path=str(f))])
    schedules.schedule_send(app, caller, message=msg, at=clock.after(hours=1))
    make_scheduler(app).tick(clock.after(hours=1, seconds=1))
    assert len(smtp_server.messages) == 1 and b"report.txt" in smtp_server.messages[0][2]


# ------------------------------------------------------------------ policy


def test_revoked_client_blocked_at_fire_time(make_app, smtp_server, clock, make_scheduler):
    from mcp_proton.domain.requests import CallerContext

    app = make_app(Preset.AUTONOMOUS, clients=[ClientConfig(client_id="hermes")])
    caller = CallerContext(client_id="hermes")
    job_id, due = schedule_in(app, caller, clock)
    app.set_policy(PolicyConfig(preset=Preset.AUTONOMOUS,
                                clients=[ClientConfig(client_id="hermes", revoked=True)]))
    rep = make_scheduler(app).tick(due + timedelta(seconds=1))
    assert rep.failed == 1 and smtp_server.messages == []
    [run] = runs(app, job_id)
    assert run.status == "blocked" and run.detail["error"]["code"] == "client_revoked"


def test_newly_denied_send_family_blocks_at_fire_time(app, caller, smtp_server, clock,
                                                      make_scheduler):
    job_id, due = schedule_in(app, caller, clock)
    app.set_policy(PolicyConfig(preset=Preset.AUTONOMOUS, rules=[
        PolicyRule(family=OperationFamily.SEND, action=Action.DENY)]))
    make_scheduler(app).tick(due + timedelta(seconds=1))
    assert smtp_server.messages == []
    assert runs(app, job_id)[0].status == "blocked"


def test_assistant_registration_pending_then_ask_at_fire(make_app, caller, owner, smtp_server,
                                                         clock, make_scheduler):
    app = make_app(Preset.ASSISTANT)
    out = schedules.schedule_send(app, caller, message=outgoing(), at=clock.after(hours=1))
    assert out.status is OperationStatus.PENDING
    assert JobStore(app.db).find() == []  # nothing registered until the owner approves

    done = app.approve(owner, out.operation_id, True, execute=True)
    assert done.status is OperationStatus.SUCCEEDED
    [job] = JobStore(app.db).find()
    assert job.client_id == caller.client_id  # the creator, not the approving owner

    make_scheduler(app).tick(clock.after(hours=1, seconds=1))
    assert smtp_server.messages == []  # Ask at fire time: the send is left pending
    [run] = runs(app, job.id)
    assert run.status == "pending"
    assert app.journal.get(run.operation_id).status is OperationStatus.PENDING


def test_reader_cannot_register(make_app, caller, clock):
    app = make_app(Preset.READER)
    with pytest.raises(MailError) as e:
        schedules.schedule_send(app, caller, message=outgoing(), at=clock.after(hours=1))
    assert e.value.code is ErrorCode.POLICY_DENIED


# ------------------------------------------------------------------ management


def test_listing_is_caller_scoped(app, caller, owner, clock):
    from mcp_proton.domain.requests import CallerContext

    job_id, _ = schedule_in(app, caller, clock)
    other = CallerContext(client_id="someone-else")
    assert common.list_jobs(app, other) == []
    assert [j["job_id"] for j in common.list_jobs(app, caller)] == [job_id]
    assert [j["job_id"] for j in common.list_jobs(app, owner)] == [job_id]
    with pytest.raises(MailError) as e:
        common.get_job(app, other, job_id)
    assert e.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(MailError):
        common.cancel_job(app, other, job_id)


def test_edit_and_cancel(app, caller, smtp_server, clock, make_scheduler):
    job_id, due = schedule_in(app, caller, clock)
    new_at = clock.after(hours=3)
    out = common.edit_job(app, caller, job_id, {"at": new_at.isoformat()})
    assert out.status is OperationStatus.SUCCEEDED
    assert JobStore(app.db).get(job_id).next_run_at == new_at.astimezone(UTC)
    sched = make_scheduler(app)
    assert sched.tick(due + timedelta(seconds=1)).fired == 0  # moved later

    with pytest.raises(MailError) as e:
        common.edit_job(app, caller, job_id, {"subject": "new"})  # content is immutable
    assert e.value.code is ErrorCode.INVALID_REQUEST

    common.edit_job(app, caller, job_id, {"enabled": False})
    assert sched.tick(new_at + timedelta(seconds=1)).fired == 0  # paused
    common.edit_job(app, caller, job_id, {"enabled": True})
    common.cancel_job(app, caller, job_id)
    assert sched.tick(new_at + timedelta(hours=1)).fired == 0
    assert smtp_server.messages == []
    assert JobStore(app.db).get(job_id).status == "cancelled"


def test_validation_errors(app, caller, clock):
    with pytest.raises(MailError):
        schedules.schedule_send(app, caller, message=outgoing(), at=clock.after(hours=-1))
    with pytest.raises(MailError):
        schedules.schedule_send(app, caller, at=clock.after(hours=1))  # no content
    with pytest.raises(MailError):
        schedules.schedule_send(app, caller, message=outgoing(), at=clock.after(hours=1),
                                recurrence={"freq": "daily", "timezone": "UTC"})
    with pytest.raises(MailError):
        schedules.schedule_send(app, caller, message=outgoing(), at=clock.after(hours=1),
                                missed_run_policy="whenever")
    assert ACCOUNT  # fixture import used
