"""Elicitation review channel for clients the owner marked as trusted.

The default review channel is the owner queue: a write that needs approval
returns ``approval_pending``. When the owner sets ``review_channel =
"elicitation"`` for a client (they trust that client to relay the question to a
person), the server asks through MCP elicitation instead, showing the exact
summary and recipients/targets recorded in the journal.

Design: the kernel is synchronous and elicitation is asynchronous, so the
operation is first created *pending* by ``MailApp.run`` without a reviewer.
Only then do we ask. The decision is recorded with ``Journal.decide`` and the
stored payload is executed by ``MailApp.resume`` (which re-checks policy), so
side effects only happen after the operation record is resolved and a tool
that restarts from the top never executes anything twice.

Two protocol eras are supported:

* handshake era: ``ctx.elicit`` inside the tool call;
* 2026-07-28 era (no server-to-client requests): the tool returns an
  ``InputRequiredResult`` carrying the elicitation request and the pending
  operation id as ``request_state``; the client answers and retries the same
  call, and the retry resolves the operation instead of creating a new one.

Elicitation that is unsupported, cancelled, times out or fails leaves the
operation pending for the owner queue. Accepting does not prove a human was
involved; that is what the owner's trust decision covers.
"""

from __future__ import annotations

import logging
from typing import Any

import anyio
from fastmcp import Context
from fastmcp.server.elicitation import (
    AcceptedElicitation,
    DeclinedElicitation,
    parse_elicit_response_type,
)
from mcp_types import (
    ClientCapabilities,
    ElicitationCapability,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
)
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from ..domain.errors import ErrorCode, MailError
from ..domain.models import OperationOutcome, OperationStatus
from ..domain.requests import CallerContext
from ..services.core import MailApp
from ..storage.journal import OperationRecord

log = logging.getLogger(__name__)

CHANNEL = "elicitation"
REVIEW_KEY = "review"
CHOICES = ["approve", "deny"]
ELICIT_TIMEOUT_SECONDS = 300.0
_BODY_PREVIEW = 400


def enabled(app: MailApp, caller: CallerContext) -> bool:
    """True when the owner trusts this client to collect the decision."""
    if caller.is_owner:
        return False
    client = app.policy.client(caller.client_id)
    return bool(client and not client.revoked and client.review_channel == CHANNEL)


def describe(rec: OperationRecord) -> str:
    """What the person is asked to approve: the journaled request, verbatim."""
    req = rec.request
    lines = [
        f"{rec.client_id} asks to: {rec.summary}",
        f"Operation: {rec.kind} on account {rec.account}",
    ]
    if req.recipients:
        lines.append("Recipients: " + ", ".join(req.recipients))
    if req.targets:
        lines.append(f"Messages affected: {len(req.targets)}")
    if req.mailboxes:
        lines.append("Mailboxes: " + ", ".join(sorted(set(req.mailboxes))))
    if req.paths:
        lines.append("Local paths: " + ", ".join(req.paths))
    payload = req.payload
    subject = payload.get("subject")
    if subject:
        lines.append(f"Subject: {subject}")
    body = payload.get("text") or payload.get("html")
    if isinstance(body, str) and body:
        more = "..." if len(body) > _BODY_PREVIEW else ""
        lines.append("Body: " + body[:_BODY_PREVIEW] + more)
    if rec.expires_at:
        lines.append(f"Expires: {rec.expires_at.isoformat()}")
    lines.append("Choose approve to carry this out exactly as shown, or deny.")
    return "\n".join(lines)


def decide(app: MailApp, caller: CallerContext, op_id: str, approve: bool | None
           ) -> OperationOutcome:
    """Record the decision and, if approved, execute the stored request."""
    if approve is None:
        return app.status(caller, op_id)
    rec = app.journal.get_for_caller(caller, op_id)
    if rec.status is OperationStatus.PENDING:
        try:
            app.journal.decide(op_id, approve, f"review:{caller.client_id}")
        except MailError as exc:  # decided concurrently (owner, expiry): report the result
            log.info("review decision not recorded: %s", exc.code)
    rec = app.journal.get_for_caller(caller, op_id)
    if approve and rec.status is OperationStatus.APPROVED:
        return app.resume(caller, op_id)
    return app.status(caller, op_id)


def supports_elicitation(ctx: Context) -> bool:
    """True when the connected client declared the elicitation capability.

    Checked before asking: in the modern era a client that cannot answer would
    turn the ``InputRequiredResult`` into an error instead of a pending result.
    """
    try:
        return bool(ctx.session.check_client_capability(
            ClientCapabilities(elicitation=ElicitationCapability())))
    except Exception:  # noqa: BLE001
        return False


def _is_modern(ctx: Context) -> bool:
    try:
        rc = ctx.request_context
        return rc is not None and rc.protocol_version in MODERN_PROTOCOL_VERSIONS
    except Exception:  # noqa: BLE001
        return False


def _choice(value: Any) -> bool | None:
    if value == "approve":
        return True
    if value == "deny":
        return False
    return None


def modern_decision(ctx: Context) -> tuple[str, bool | None] | None:
    """On the retry round: ``(operation_id, decision)`` from the client's answer."""
    try:
        responses = ctx.input_responses
        state = ctx.request_state
    except Exception:  # noqa: BLE001
        return None
    if not responses or not state or REVIEW_KEY not in responses:
        return None
    answer = responses[REVIEW_KEY]
    if not isinstance(answer, ElicitResult):
        return state, None
    if answer.action == "decline":
        return state, False
    if answer.action != "accept":
        return state, None
    return state, _choice((answer.content or {}).get("value"))


def _input_required(rec: OperationRecord) -> InputRequiredResult:
    config = parse_elicit_response_type(CHOICES, response_title="Decision")
    request = ElicitRequest(params=ElicitRequestFormParams(
        message=describe(rec), requested_schema=config.schema))
    return InputRequiredResult(input_requests={REVIEW_KEY: request}, request_state=rec.id)


async def _ask(ctx: Context, rec: OperationRecord) -> bool | None:
    try:
        with anyio.fail_after(ELICIT_TIMEOUT_SECONDS):
            result = await ctx.elicit(describe(rec), CHOICES, response_title="Decision")
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, anyio.get_cancelled_exc_class()):
            raise
        log.info("elicitation unavailable (%s); operation stays pending", type(exc).__name__)
        return None
    if isinstance(result, AcceptedElicitation):
        return _choice(result.data)
    if isinstance(result, DeclinedElicitation):
        return False
    return None  # cancelled


async def offer(ctx: Context, app: MailApp, caller: CallerContext, outcome: OperationOutcome
                ) -> OperationOutcome | InputRequiredResult:
    """Ask the trusted client about a pending ``outcome``.

    Returns the resolved outcome, the unchanged pending one, or an
    ``InputRequiredResult`` (modern protocol) for the client to answer.
    """
    if outcome.status is not OperationStatus.PENDING or not enabled(app, caller):
        return outcome
    if not supports_elicitation(ctx):
        return outcome
    rec = app.journal.get_for_caller(caller, outcome.operation_id)
    if _is_modern(ctx):
        return _input_required(rec)
    decision = await _ask(ctx, rec)
    if decision is None:
        return outcome
    return await anyio.to_thread.run_sync(decide, app, caller, rec.id, decision)


def require_enabled(app: MailApp, caller: CallerContext) -> None:
    if not enabled(app, caller):
        raise MailError(ErrorCode.POLICY_DENIED,
                        "elicitation review is not enabled for this client")
