"""events_*: change journal, watch status and conversation presentation."""

from typing import Annotated, Any

from fastmcp.server.providers import LocalProvider
from pydantic import Field

from ...services import conversations, events
from .base import AccountParam, Gateway, HandleParam, MailboxParam


def build(gw: Gateway) -> LocalProvider:
    app, p = gw.app, gw.provider()
    Limit = gw.limit_type()  # noqa: N806 - a type alias

    @gw.tool(p, "events_list",
             "Catch up on mailbox changes (new mail, flag changes, deletions). Pass the last "
             "next_cursor you saw; notifications are hints, this journal is the source.",
             title="List events")
    def events_list(
        account: AccountParam,
        cursor: Annotated[int | None, Field(
            ge=0, description="Last seq seen; omit for the start.")] = None,
        limit: Limit = 100,  # type: ignore[valid-type]
        types: Annotated[list[str] | None, Field(max_length=20)] = None,
    ) -> dict[str, Any]:
        return gw.read(lambda c: events.list_events(app, c, account, cursor, limit, types))

    @gw.tool(p, "events_watch_status",
             "Watch scope, polling interval, last reconciliation and freshness per mailbox.",
             title="Watch status")
    def events_watch_status(account: AccountParam) -> dict[str, Any]:
        return gw.read(lambda c: events.watch_status(app, c, account))

    @gw.tool(p, "events_reconcile",
             "Compare one mailbox (or the whole watch scope) with the last known state now and "
             "record changes in the journal.", title="Reconcile now", idempotent=True)
    def events_reconcile(
        account: AccountParam,
        mailbox: Annotated[MailboxParam | None, Field(description="Default: whole scope.")] = None,
    ) -> dict[str, Any]:
        return gw.read(lambda c: events.reconcile_now(app, c, account, mailbox))

    @gw.tool(p, "conversations_get",
             "Thread that contains a message, built from Message-ID/References (presentation "
             "only; heuristic grouping is flagged).", title="Get conversation")
    def conversations_get(
        handle: HandleParam,
        mailboxes: Annotated[list[str] | None, Field(max_length=50)] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        return gw.read(lambda c: conversations.get_conversation(app, c, handle, mailboxes, limit))

    return p
