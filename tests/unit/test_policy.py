from datetime import UTC, datetime, timedelta

import pytest

from mcp_proton.domain.families import OperationFamily as F
from mcp_proton.domain.requests import CallerContext, OperationRequest
from mcp_proton.policy.engine import evaluate, explain
from mcp_proton.policy.model import (
    Action,
    ClientConfig,
    Constraints,
    PolicyConfig,
    PolicyRule,
    Preset,
)

C = CallerContext(client_id="hermes")


def req(family=F.READ, mailboxes=("INBOX",), **kw):
    return OperationRequest(kind="x", family=family, account="me", mailboxes=list(mailboxes), **kw)


def test_not_onboarded_denies_everything():
    d = evaluate(PolicyConfig(), C, req())
    assert d.action is Action.DENY and d.code == "not_onboarded"


@pytest.mark.parametrize(
    "preset,family,expected",
    [
        (Preset.READER, F.READ, Action.ALLOW),
        (Preset.READER, F.ORGANIZE, Action.DENY),
        (Preset.READER, F.EXPORT, Action.DENY),
        (Preset.ASSISTANT, F.ORGANIZE, Action.ALLOW),
        (Preset.ASSISTANT, F.DRAFTS, Action.ALLOW),
        (Preset.ASSISTANT, F.SEND, Action.ASK),
        (Preset.ASSISTANT, F.PERMANENT_DELETE, Action.ASK),
        (Preset.ASSISTANT, F.IMPORT, Action.ASK),
        (Preset.AUTONOMOUS, F.SEND, Action.ALLOW),
        (Preset.AUTONOMOUS, F.FOLDER_DELETE, Action.ALLOW),
    ],
)
def test_presets(preset, family, expected):
    assert evaluate(PolicyConfig(preset=preset), C, req(family)).action is expected


def test_custom_missing_family_is_denied():
    p = PolicyConfig(preset=Preset.CUSTOM, custom_baseline={F.READ: Action.ALLOW})
    assert evaluate(p, C, req(F.SEND)).action is Action.DENY


def test_most_specific_rule_wins_and_ties_prefer_deny():
    p = PolicyConfig(
        preset=Preset.ASSISTANT,
        rules=[
            PolicyRule(family=F.SEND, action=Action.ALLOW, client="hermes"),
            PolicyRule(family=F.SEND, action=Action.DENY, account="me"),
            PolicyRule(family=F.ORGANIZE, action=Action.DENY, client="hermes", mailbox="Labels/*"),
            PolicyRule(family=F.ORGANIZE, action=Action.ALLOW, account="me", mailbox="Labels/*"),
        ],
    )
    # equal specificity (1 each): deny wins
    assert evaluate(p, C, req(F.SEND)).action is Action.DENY
    assert evaluate(p, C, req(F.ORGANIZE, ["Labels/x"])).action is Action.DENY
    assert evaluate(p, C, req(F.ORGANIZE, ["INBOX"])).action is Action.ALLOW


def test_multi_scope_any_deny_rejects_any_ask_requires_review():
    p = PolicyConfig(
        preset=Preset.AUTONOMOUS,
        rules=[
            PolicyRule(family=F.ORGANIZE, action=Action.ASK, mailbox="Archive"),
            PolicyRule(family=F.ORGANIZE, action=Action.DENY, mailbox="Folders/Secret"),
        ],
    )
    assert evaluate(p, C, req(F.ORGANIZE, ["INBOX", "Archive"])).action is Action.ASK
    assert evaluate(p, C, req(F.ORGANIZE, ["Archive", "Folders/Secret"])).action is Action.DENY


def test_temporary_grant_expires():
    past = datetime.now(UTC) - timedelta(minutes=1)
    future = datetime.now(UTC) + timedelta(minutes=5)
    p = PolicyConfig(preset=Preset.ASSISTANT,
                     rules=[PolicyRule(family=F.SEND, action=Action.ALLOW, client="hermes",
                                       expires_at=past)])
    assert evaluate(p, C, req(F.SEND)).action is Action.ASK
    p.rules[0].expires_at = future
    assert evaluate(p, C, req(F.SEND)).action is Action.ALLOW


def test_constraints_cannot_be_broadened_by_allow():
    p = PolicyConfig(preset=Preset.AUTONOMOUS,
                     constraints=Constraints(allowed_recipients=["@example.com"]))
    ok = evaluate(p, C, req(F.SEND, recipients=["a@example.com"]))
    bad = evaluate(p, C, req(F.SEND, recipients=["a@example.com", "x@evil.test"]))
    assert ok.action is Action.ALLOW
    assert bad.action is Action.DENY and bad.code == "constraint_violation"


def test_client_constraints_intersect():
    p = PolicyConfig(preset=Preset.AUTONOMOUS,
                     clients=[ClientConfig(client_id="hermes",
                                           constraints=Constraints(max_batch_size=5))])
    assert evaluate(p, C, req(F.ORGANIZE, batch_size=6)).action is Action.DENY
    other = CallerContext(client_id="other")
    assert evaluate(p, other, req(F.ORGANIZE, batch_size=6)).action is Action.ALLOW


def test_revoked_and_paused():
    p = PolicyConfig(preset=Preset.AUTONOMOUS, paused_accounts=["me"])
    assert evaluate(p, C, req()).code == "account_paused"
    p = PolicyConfig(preset=Preset.AUTONOMOUS,
                     clients=[ClientConfig(client_id="hermes", revoked=True)])
    assert evaluate(p, C, req()).code == "client_revoked"


def test_paths_must_be_within_configured_dirs(tmp_path):
    inside = tmp_path / "in" / "a.txt"
    inside.parent.mkdir()
    inside.write_text("x")
    p = PolicyConfig(preset=Preset.AUTONOMOUS,
                     constraints=Constraints(export_dirs=[str(tmp_path / "in")]))
    assert evaluate(p, C, req(F.EXPORT, paths=[str(inside)])).action is Action.ALLOW
    d = evaluate(p, C, req(F.EXPORT, paths=[str(tmp_path / "in" / ".." / "out.txt")]))
    assert d.action is Action.DENY and d.code == "path_rejected"


def test_also_families_checked():
    p = PolicyConfig(preset=Preset.ASSISTANT)
    r = req(F.DRAFTS, payload={"_also_families": ["attachment_ingest"]})
    assert evaluate(p, C, r).action is Action.ASK


def test_explain_reports_sources():
    p = PolicyConfig(preset=Preset.ASSISTANT,
                     rules=[PolicyRule(family=F.SEND, action=Action.DENY, client="hermes")])
    out = explain(p, "hermes", "me")
    assert out["send"]["action"] == "deny" and "client=hermes" in out["send"]["source"]
    assert out["read"]["source"] == "preset assistant"
