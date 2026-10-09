"""Local rules: matching, preview (read-only), manual run, trigger on new mail."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.jobs.conftest import ACCOUNT
from tests.services.conftest import make_raw

from mcp_proton.domain.errors import MailError
from mcp_proton.domain.models import Address, MessageHandle, MessageSummary, OperationStatus
from mcp_proton.jobs import common, rules
from mcp_proton.jobs.store import JobStore
from mcp_proton.policy.model import Preset
from mcp_proton.services import events, mailboxes


def uids(app, mailbox):
    return app.store(ACCOUNT).list_uids(mailbox)[1]


def summary(**kw) -> MessageSummary:
    base = {"handle": "h", "mailbox": "INBOX", "uid": 1,
            "from": [Address(name="News Bot", email="news@Example.org")],
            "to": [Address(email="me@x.test")], "subject": "Weekly Digest",
            "has_attachments": False}
    base.update(kw)
    return MessageSummary.model_validate(base)


def test_matching_conditions():
    s = summary()
    m = rules.RuleMatch
    assert rules.matches(m(sender_contains="news bot"), s)
    assert rules.matches(m(sender_domain="example.org"), s)
    assert rules.matches(m(sender_domain="@EXAMPLE.org"), s)
    assert not rules.matches(m(sender_domain="ample.org"), s)  # whole domain, not a suffix
    assert rules.matches(m(subject_contains="digest"), s)
    assert rules.matches(m(to_contains="me@x"), s)
    assert rules.matches(m(has_attachments=False), s)
    assert not rules.matches(m(has_attachments=True), s)
    assert not rules.matches(m(has_attachments=True), summary(has_attachments=None))
    assert not rules.matches(m(sender_domain="example.org", subject_contains="invoice"), s)
    with pytest.raises(ValueError):
        m()  # a rule needs a condition


@pytest.fixture
def mail(app, owner, seed):
    mailboxes.create_folder(app, owner, ACCOUNT, "News")
    mailboxes.create_label(app, owner, ACCOUNT, "Digest")
    news = seed(n=2, frm="news@example.org", subject="Weekly digest")
    other = seed(n=1, frm="bob@friends.test", subject="Lunch?")
    return news, other


RULE = {
    "name": "file newsletters", "match": {"sender_domain": "example.org"},
    "actions": [{"type": "move", "target": "Folders/News"},
                {"type": "label", "target": "Digest"},
                {"type": "flags", "add": ["read"]}],
}


def create_rule(app, caller, rule=None, **extra):
    out = rules.create(app, caller, ACCOUNT, {**(rule or RULE), **extra})
    assert out.status is OperationStatus.SUCCEEDED, out
    return out.result["job_id"]


def test_preview_is_read_only_and_shows_policy(app, caller, mail):
    news, other = mail
    before = {mb: uids(app, mb) for mb in ("INBOX", "Folders/News")}
    pv = rules.preview(app, caller, ACCOUNT, RULE)
    assert pv["matched"] == 2 and pv["scanned"] == 2  # scanned after the server-side prefilter
    assert {m["handle"] for m in pv["messages"]} == set(news)
    assert [p["action"] for p in pv["planned_operations"]] == ["flags", "label", "move"]
    assert all(p["policy"]["action"] == "allow" for p in pv["planned_operations"])
    assert "server-side filters" in pv["note"]
    assert before == {mb: uids(app, mb) for mb in ("INBOX", "Folders/News")}
    assert all(o.kind != "messages.move" for o in app.journal.list())


def test_preview_under_assistant_reports_effective_action(make_app, caller, owner, seed):
    app = make_app(Preset.ASSISTANT)
    mailboxes.create_folder(app, owner, ACCOUNT, "News")
    app.store(ACCOUNT).append("INBOX", make_raw(frm="news@example.org"))
    pv = rules.preview(app, caller, ACCOUNT, {**RULE, "actions": RULE["actions"][:1]})
    assert pv["matched"] == 1 and pv["planned_operations"][0]["policy"]["action"] == "allow"


def test_manual_run_applies_actions_and_records_history(app, caller, mail):
    news, other = mail
    rule_id = create_rule(app, caller)
    res = rules.run(app, caller, rule_id)
    assert res["matched"] == 2 and res["status"] == "succeeded"
    assert [r["action"] for r in res["results"]] == ["flags", "label", "move"]
    assert len(uids(app, "Folders/News")) == 2 and len(uids(app, "Labels/Digest")) == 2
    assert len(uids(app, "INBOX")) == 1  # the unrelated message stays
    [run] = JobStore(app.db).runs(rule_id)
    assert run.detail["manual"] is True and run.detail["matched"] == 2
    # a second run finds nothing left to do
    assert rules.run(app, caller, rule_id)["matched"] == 0


def test_run_under_assistant_leaves_ask_pending(make_app, caller, owner):
    app = make_app(Preset.ASSISTANT)
    mailboxes.create_folder(app, owner, ACCOUNT, "News")
    app.store(ACCOUNT).append("INBOX", make_raw(frm="news@example.org"))
    out = rules.create(app, caller, ACCOUNT, {**RULE, "actions": RULE["actions"][:1]})
    assert out.status is OperationStatus.PENDING  # registration itself asks
    app.approve(owner, out.operation_id, True, execute=True)
    [job] = JobStore(app.db).find()
    res = rules.run(app, caller, job.id)  # organizing is Allow under Assistant
    assert res["status"] == "succeeded"


def test_validation(app, caller):
    with pytest.raises(MailError):
        rules.create(app, caller, ACCOUNT, {**RULE, "actions": [{"type": "move",
                                                                 "target": "Folders/Nope"}]})
    with pytest.raises(MailError):
        rules.create(app, caller, ACCOUNT, {**RULE, "match": {}})
    with pytest.raises(MailError):
        rules.create(app, caller, ACCOUNT, {**RULE, "actions": [
            {"type": "move", "target": "Archive"}, {"type": "move", "target": "Archive"}]})


def test_trigger_applies_to_new_mail_only(app, caller, owner, seed, mail, clock, make_scheduler):
    rule_id = create_rule(app, caller, trigger=True)
    rec = events.Reconciler(app)
    rec.reconcile(ACCOUNT, "INBOX")  # baseline: existing mail emits nothing
    sched = make_scheduler(app)
    sched.tick(clock.after(seconds=1))
    assert len(uids(app, "INBOX")) == 3  # pre-existing mail is untouched

    new_news = seed(frm="digest@example.org", subject="Fresh digest")
    new_other = seed(frm="carol@friends.test", subject="Hi")
    rec.reconcile(ACCOUNT, "INBOX")
    rep = sched.tick(clock.after(seconds=2))
    assert rep.rule_batches == 1
    assert len(uids(app, "Folders/News")) == 1 and len(uids(app, "Labels/Digest")) == 1
    assert len(uids(app, "INBOX")) == 4  # 3 old + carol; the new newsletter was filed
    assert new_news and new_other

    runs = JobStore(app.db).runs(rule_id)
    assert runs[0].detail["trigger"] is True and runs[0].detail["matched"] == 1
    # the cursor advanced: the same events are not processed twice
    assert sched.tick(clock.after(seconds=3)).rule_batches == 0


def test_trigger_blocked_for_revoked_client(make_app, owner, seed, clock, make_scheduler):
    from mcp_proton.domain.requests import CallerContext
    from mcp_proton.policy.model import ClientConfig, PolicyConfig

    app = make_app(Preset.AUTONOMOUS, clients=[ClientConfig(client_id="hermes")])
    mailboxes.create_folder(app, owner, ACCOUNT, "News")
    caller = CallerContext(client_id="hermes")
    rule_id = create_rule(app, caller, {**RULE, "actions": RULE["actions"][:1]}, trigger=True)
    rec = events.Reconciler(app)
    rec.reconcile(ACCOUNT, "INBOX")
    app.set_policy(PolicyConfig(preset=Preset.AUTONOMOUS,
                                clients=[ClientConfig(client_id="hermes", revoked=True)]))
    app.store(ACCOUNT).append("INBOX", make_raw(frm="news@example.org"))
    rec.reconcile(ACCOUNT, "INBOX")
    make_scheduler(app).tick(clock_now())
    assert len(uids(app, "INBOX")) == 1  # nothing was filed
    assert JobStore(app.db).runs(rule_id)[0].status == "blocked"


def clock_now():
    return datetime.now(UTC) + timedelta(seconds=1)


def test_rules_are_caller_scoped(app, caller, mail):
    from mcp_proton.domain.requests import CallerContext

    rule_id = create_rule(app, caller)
    with pytest.raises(MailError):
        rules.run(app, CallerContext(client_id="other"), rule_id)
    with pytest.raises(MailError):
        rules.get_rule(app, caller, "job_missing")
    assert common.list_jobs(app, caller, job_type="rule")[0]["job_id"] == rule_id


def test_edit_rule(app, caller, mail):
    rule_id = create_rule(app, caller)
    common.edit_job(app, caller, rule_id, {"match": {"sender_domain": "friends.test"}})
    pv = rules.preview(app, caller, ACCOUNT, JobStore(app.db).get(rule_id).spec["rule"])
    assert pv["matched"] == 1
    with pytest.raises(MailError):
        common.edit_job(app, caller, rule_id, {"actions": []})
    assert MessageHandle.parse(pv["messages"][0]["handle"]).mailbox == "INBOX"
