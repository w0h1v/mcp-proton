"""Elicitation review channel, in both protocol eras.

``legacy`` clients answer ``elicitation/create`` during the tool call; modern
(2026-07-28) clients answer an ``InputRequiredResult`` and retry the call. The
FastMCP client drives that retry itself, so tests only see the final result.
"""

from __future__ import annotations

import pytest
from fastmcp.client.elicitation import ElicitResult

from conftest import ACCOUNT, call, client_for  # type: ignore[import-not-found]
from mcp_proton.policy.model import ClientConfig, Preset

SEND = {"account": ACCOUNT, "to": ["bob@example.com"], "subject": "Budget",
        "text": "numbers inside"}
MODES = ["legacy", "auto"]


@pytest.fixture
def trusted(make_app):
    def _make(channel: str = "elicitation"):
        return make_app(Preset.ASSISTANT,
                        clients=[ClientConfig(client_id="hermes", review_channel=channel)])

    return _make


class Asked:
    def __init__(self, answer: str | None, action: str = "accept") -> None:
        self.answer, self.action, self.messages = answer, action, []

    async def __call__(self, message, response_type, params, context):
        self.messages.append(message)
        if self.action != "accept":
            return ElicitResult(action=self.action)
        return ElicitResult(action="accept", content={"value": self.answer})


@pytest.mark.parametrize("mode", MODES)
async def test_accept_executes_once(trusted, smtp_server, mode):
    app, handler = trusted(), Asked("approve")
    async with client_for(app, elicitation_handler=handler, mode=mode) as c:
        out = await call(c, "mail_send", SEND)
    assert out["status"] == "succeeded", out
    assert len(smtp_server.messages) == 1
    # the person saw the exact summary and recipients
    [shown] = handler.messages
    assert "bob@example.com" in shown and "Budget" in shown and "numbers inside" in shown
    rec = app.journal.get_for_caller(app_caller(), out["operation_id"])
    assert rec.decided_by == "review:hermes"


@pytest.mark.parametrize("mode", MODES)
async def test_deny_choice_is_denied(trusted, smtp_server, mode):
    app = trusted()
    async with client_for(app, elicitation_handler=Asked("deny"), mode=mode) as c:
        out = await call(c, "mail_send", SEND)
        assert out["status"] == "denied"
        resumed = await call(c, "operations_resume", {"operation_id": out["operation_id"]})
        assert resumed["status"] == "denied"
    assert smtp_server.messages == []


@pytest.mark.parametrize("mode", MODES)
async def test_decline_is_denied(trusted, smtp_server, mode):
    async with client_for(trusted(), elicitation_handler=Asked(None, "decline"), mode=mode) as c:
        out = await call(c, "mail_send", SEND)
    assert out["status"] == "denied"
    assert smtp_server.messages == []


@pytest.mark.parametrize("mode", MODES)
async def test_cancel_leaves_pending(trusted, smtp_server, mode):
    async with client_for(trusted(), elicitation_handler=Asked(None, "cancel"), mode=mode) as c:
        out = await call(c, "mail_send", SEND)
    assert out["status"] == "approval_pending"
    assert smtp_server.messages == []


@pytest.mark.parametrize("mode", MODES)
async def test_client_without_elicitation_stays_pending(trusted, smtp_server, mode):
    async with client_for(trusted(), mode=mode) as c:
        out = await call(c, "mail_send", SEND)
    assert out["status"] == "approval_pending", out
    assert smtp_server.messages == []


@pytest.mark.parametrize("mode", MODES)
async def test_queue_channel_never_asks(trusted, smtp_server, mode):
    handler = Asked("approve")
    async with client_for(trusted("queue"), elicitation_handler=handler, mode=mode) as c:
        out = await call(c, "mail_send", SEND)
    assert out["status"] == "approval_pending"
    assert handler.messages == [] and smtp_server.messages == []


@pytest.mark.parametrize("mode", MODES)
async def test_policy_still_applies_after_accept(trusted, smtp_server, mode):
    """Accepting cannot widen policy: a recipient outside the allow-list is denied outright."""
    from mcp_proton.policy.model import Constraints

    app = trusted()
    app.set_policy(app.policy.model_copy(update={
        "constraints": Constraints(allowed_recipients=["@corp.test"])}))
    handler = Asked("approve")
    async with client_for(app, elicitation_handler=handler, mode=mode) as c:
        out = await call(c, "mail_send", SEND)
    assert out["error"]["code"] == "constraint_violation"
    assert handler.messages == [] and smtp_server.messages == []


async def test_pending_resume_offers_elicitation(trusted, smtp_server):
    """A pending operation left by an unattended call can be resolved on resume."""
    app = trusted()
    async with client_for(app, mode="legacy") as c:
        out = await call(c, "mail_send", SEND)
    assert out["status"] == "approval_pending"
    async with client_for(app, elicitation_handler=Asked("approve"), mode="legacy") as c:
        done = await call(c, "operations_resume", {"operation_id": out["operation_id"]})
    assert done["status"] == "succeeded"
    assert len(smtp_server.messages) == 1


async def test_retry_with_forged_answer_cannot_touch_other_clients_ops(trusted, smtp_server):
    app = trusted()
    async with client_for(app, mode="legacy") as c:
        out = await call(c, "mail_send", SEND)
    from mcp_proton.domain.requests import CallerContext
    from mcp_proton.mcp import review

    other = CallerContext(client_id="intruder")
    from mcp_proton.domain.errors import MailError

    with pytest.raises(MailError):
        review.decide(app, other, out["operation_id"], True)
    assert smtp_server.messages == []


def app_caller():
    from mcp_proton.domain.requests import CallerContext

    return CallerContext(client_id="hermes")
