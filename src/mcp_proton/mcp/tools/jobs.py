"""jobs_*, rules_*, webhooks_* and operations_undo: the optional local automation layer.

Everything here is local automation run by this service (not Proton features). Registering,
editing or cancelling is a write of policy family AUTOMATION (Ask under Assistant, denied
under Reader); when a job fires, the mail action is checked against policy again as the
client that created it.
"""

from typing import Annotated, Any

from fastmcp import Context
from fastmcp.server.providers import LocalProvider
from pydantic import Field

from ...domain.models import OutgoingMessage
from ...jobs import common, reminders, rules, schedules, undo, webhooks
from .base import (
    Access,
    AccountParam,
    AddressInput,
    Gateway,
    HandleParam,
    HandlesParam,
    IdempotencyParam,
    OperationIdParam,
    WriteResult,
    addresses,
    invalid,
)
from .drafts import AttachmentItems

JobIdParam = Annotated[str, Field(min_length=1, max_length=64,
                                  description="job_id from jobs_list.")]
WhenParam = Annotated[str, Field(description=(
    "ISO-8601 timestamp, e.g. 2026-10-12T09:00:00+02:00. Without an offset, also pass "
    "`timezone` (IANA name such as Europe/Berlin)."))]
TimezoneParam = Annotated[str | None, Field(description="IANA timezone for a timestamp "
                                                       "without an offset.")]
LOCAL = (" Local automation: it needs this machine and the mcp-proton service to be running "
         "at that time.")


def build(gw: Gateway) -> LocalProvider:
    app, p = gw.app, gw.provider()
    Limit = gw.limit_type()  # noqa: N806 - a type alias

    # ---------------------------------------------------------------- scheduled sends
    @gw.tool(p, "jobs_schedule_send",
             "Schedule a message to be sent later, once (`at`) or repeatedly (`recurrence`: "
             "{freq: daily|weekly, time: 'HH:MM', weekdays: [0-6, Monday=0], timezone}). "
             "Pass the message content (frozen at registration) or `draft` (a draft handle "
             "bound to the draft's current content: if the draft is edited or removed before "
             "it fires, the run fails and nothing is sent). This is a LOCAL schedule, not "
             "Proton's native scheduled send." + LOCAL +
             " missed_run_policy decides what happens after downtime: run_once, skip or "
             "run_all (capped). Policy for sending is checked again when it fires.",
             access=Access.WRITE, title="Schedule send")
    async def jobs_schedule_send(
        ctx: Context, account: AccountParam, at: WhenParam | None = None,
        timezone: TimezoneParam = None, recurrence: dict[str, Any] | None = None,
        to: Annotated[list[AddressInput] | None, Field(max_length=100)] = None,
        subject: str = "", text: str | None = None, html: str | None = None,
        cc: list[AddressInput] | None = None, bcc: list[AddressInput] | None = None,
        reply_to: list[AddressInput] | None = None,
        from_address: AddressInput | None = None,
        attachments: AttachmentItems | None = None,
        draft: Annotated[HandleParam | None, Field(
            description="Instead of content: handle of a draft to send as it is now.")] = None,
        missed_run_policy: Annotated[str, Field(
            description="run_once | skip | run_all")] = "run_once",
        idempotency_key: IdempotencyParam = None,
    ) -> WriteResult:
        def run(c: Any) -> Any:
            message = None
            if draft is None:
                if not to:
                    raise invalid("`to` is required unless `draft` is given")
                message = OutgoingMessage.model_validate({
                    "account": account,
                    "from": addresses([from_address])[0] if from_address else None,
                    "to": addresses(to), "cc": addresses(cc), "bcc": addresses(bcc),
                    "reply_to": addresses(reply_to), "subject": subject, "text": text,
                    "html": html, "attachments": attachments or []})
            return schedules.schedule_send(
                app, c, message=message, draft=draft, at=at, timezone=timezone,
                recurrence=recurrence, missed_run_policy=missed_run_policy,
                idempotency_key=idempotency_key)

        return await gw.write(ctx, run)

    @gw.tool(p, "jobs_list",
             "List your scheduled sends, reminders, snoozes and rules with status, next run "
             "and last result. A client sees only its own jobs." + LOCAL,
             title="List jobs")
    def jobs_list(
        type: Annotated[str | None, Field(  # noqa: A002
            description="scheduled_send | reminder | snooze | rule")] = None,
        status: Annotated[str | None, Field(
            description="active | paused | done | cancelled | failed")] = None,
        account: AccountParam | None = None, limit: Limit = 50,
    ) -> dict[str, Any]:
        return gw.read(lambda c: {"items": common.list_jobs(
            app, c, job_type=type, status=status, account=account, limit=limit)})

    @gw.tool(p, "jobs_get", "One job with its recent runs (status, operation_id, reason).",
             title="Get job")
    def jobs_get(job_id: JobIdParam,
                 runs: Annotated[int, Field(ge=0, le=100)] = 10) -> dict[str, Any]:
        return gw.read(lambda c: common.get_job(app, c, job_id, runs))

    @gw.tool(p, "jobs_edit",
             "Change a job's timing or enabled state. Scheduled sends: at, timezone, recurrence, "
             "missed_run_policy, enabled (message content is immutable: cancel and re-create). "
             "Reminders: at, note, star, enabled. Snoozes: until, enabled. Rules: name, match, "
             "actions, trigger, trigger_mailboxes, enabled.",
             access=Access.WRITE, idempotent=True, title="Edit job")
    async def jobs_edit(
        ctx: Context, job_id: JobIdParam,
        changes: Annotated[dict[str, Any], Field(description="Fields to change.")],
    ) -> WriteResult:
        return await gw.write(ctx, lambda c: common.edit_job(app, c, job_id, changes))

    @gw.tool(p, "jobs_cancel",
             "Cancel a job. Cancelling a snooze does not move its messages back.",
             access=Access.WRITE, idempotent=True, title="Cancel job")
    async def jobs_cancel(ctx: Context, job_id: JobIdParam) -> WriteResult:
        return await gw.write(ctx, lambda c: common.cancel_job(app, c, job_id))

    # ---------------------------------------------------------------- snooze / remind
    @gw.tool(p, "jobs_snooze",
             "Snooze messages: file them into `folder` (Archive or a folder under Folders/) "
             "now and move them back to where they were at `until`. LOCAL automation, not "
             "Proton's native snooze." + LOCAL +
             " The result's `filing` shows what was filed now.",
             access=Access.WRITE, title="Snooze messages")
    async def jobs_snooze(
        ctx: Context, handles: HandlesParam, until: WhenParam,
        folder: Annotated[str, Field(description="Archive or a folder under Folders/.")],
        timezone: TimezoneParam = None,
    ) -> WriteResult:
        return await gw.write(ctx, lambda c: reminders.snooze(
            app, c, handles, until, folder, timezone=timezone))

    @gw.tool(p, "jobs_remind",
             "Remind about a message: at `at`, a reminder_due event is added to the event "
             "journal (events_list) and, if `star`, the message is starred (policy checked "
             "then). LOCAL automation." + LOCAL,
             access=Access.WRITE, title="Remind me")
    async def jobs_remind(
        ctx: Context, handle: HandleParam, at: WhenParam,
        note: Annotated[str, Field(max_length=500)] = "", timezone: TimezoneParam = None,
        star: bool = False,
    ) -> WriteResult:
        return await gw.write(ctx, lambda c: reminders.remind(
            app, c, handle, at, note, timezone=timezone, star=star))

    # ---------------------------------------------------------------- rules
    @gw.tool(p, "rules_create",
             "Register a local rule. match (all given conditions must hold): sender_contains, "
             "sender_domain, subject_contains, to_contains, has_attachments. actions: "
             "[{type: move, target: 'Folders/X'}, {type: label, target: 'Labels/Y'}, "
             "{type: flags, add: ['read','star'], remove: [...]}]. Optional `trigger` also "
             "applies it to new mail. Local only: this does not edit Proton's server-side "
             "filters. Use rules_preview first.",
             access=Access.WRITE, title="Create rule")
    async def rules_create(
        ctx: Context, account: AccountParam,
        name: Annotated[str, Field(min_length=1, max_length=80)],
        match: dict[str, Any], actions: Annotated[list[dict[str, Any]], Field(min_length=1)],
        mailbox: Annotated[str, Field(description="Scope of previews and manual runs.")] = "INBOX",
        trigger: bool = False,
        trigger_mailboxes: list[str] | None = None,
    ) -> WriteResult:
        rule: dict[str, Any] = {"name": name, "mailbox": mailbox, "match": match,
                                "actions": actions, "trigger": trigger}
        if trigger_mailboxes is not None:
            rule["trigger_mailboxes"] = trigger_mailboxes
        return await gw.write(ctx, lambda c: rules.create(app, c, account, rule))

    @gw.tool(p, "rules_preview",
             "Read-only preview of a rule (a registered `rule_id`, or an inline `rule` "
             "with account): matching messages, the planned operations and the effective "
             "policy action for each. Nothing is changed.", title="Preview rule")
    def rules_preview(
        rule_id: Annotated[str | None, Field(description="A registered rule's job_id.")] = None,
        account: AccountParam | None = None,
        rule: Annotated[dict[str, Any] | None, Field(
            description="Inline rule: name, match, actions (as in rules_create).")] = None,
        mailbox: str | None = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 200,
    ) -> dict[str, Any]:
        def run(c: Any) -> Any:
            if (rule_id is None) == (rule is None):
                raise invalid("pass exactly one of rule_id or rule")
            if rule_id is not None:
                job = rules.get_rule(app, c, rule_id)
                return rules.preview(app, c, job.account, job.spec["rule"], mailbox, limit)
            if not account:
                raise invalid("`account` is required with an inline rule")
            return rules.preview(app, c, account, rule or {}, mailbox, limit)

        return gw.read(run)

    @gw.tool(p, "rules_run",
             "Run a registered rule now over a mailbox. Every action is a normal organizing "
             "write under your policy (Ask leaves operations pending). Recorded in the rule's "
             "run history.", access=Access.WRITE, title="Run rule")
    def rules_run(
        rule_id: JobIdParam, mailbox: str | None = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 200,
    ) -> dict[str, Any]:
        return gw.read(lambda c: rules.run(app, c, rule_id, mailbox, limit))

    # ---------------------------------------------------------------- webhooks
    @gw.tool(p, "webhooks_create",
             "Register a webhook for event notifications (metadata only: handles, UIDs, flags, "
             "mailbox names; never subjects or bodies). https only (plain http only for "
             "loopback). Requests carry X-MCP-Proton-Signature: sha256=HMAC-SHA256(secret, "
             "'<X-MCP-Proton-Timestamp>.' + body). The secret is returned once. Notifications "
             "are hints; use events_list to catch up.",
             access=Access.WRITE, title="Create webhook")
    async def webhooks_create(
        ctx: Context, account: AccountParam, url: str,
        event_types: Annotated[list[str] | None, Field(
            max_length=20, description="Default: all event types.")] = None,
    ) -> WriteResult:
        return await gw.write(ctx, lambda c: webhooks.create_with_secret(
            app, c, account, url, event_types))

    @gw.tool(p, "webhooks_reveal_secret",
             "The signing secret of a webhook you created, exactly once (use this if "
             "webhooks_create was pending approval).", access=Access.WRITE, approval=False,
             title="Reveal webhook secret")
    def webhooks_reveal_secret(
        webhook_id: Annotated[str, Field(min_length=1, max_length=64)],
    ) -> dict[str, Any]:
        return gw.read(lambda c: {"webhook_id": webhook_id,
                                  "secret": webhooks.reveal_secret(app, c, webhook_id)})

    @gw.tool(p, "webhooks_list", "Your webhooks with delivery status (never the secret).",
             title="List webhooks")
    def webhooks_list() -> dict[str, Any]:
        return gw.read(lambda c: {"items": webhooks.list_webhooks(app, c)})

    @gw.tool(p, "webhooks_delete", "Delete a webhook.", access=Access.WRITE, idempotent=True,
             title="Delete webhook")
    async def webhooks_delete(
        ctx: Context, webhook_id: Annotated[str, Field(min_length=1, max_length=64)],
    ) -> WriteResult:
        return await gw.write(ctx, lambda c: webhooks.delete(app, c, webhook_id))

    # ---------------------------------------------------------------- undo
    @gw.tool(p, "operations_undo",
             "Best-effort undo of one of your completed operations: moves back, flag changes "
             "and applied labels, using the state recorded at the time. It runs as new "
             "operations under your policy. Sends, permanent deletion, draft discards, imports "
             "and exports cannot be undone. Items that changed since fail individually.",
             access=Access.WRITE, title="Undo operation")
    def operations_undo(operation_id: OperationIdParam) -> dict[str, Any]:
        return gw.read(lambda c: undo.undo(app, c, operation_id))

    return p
