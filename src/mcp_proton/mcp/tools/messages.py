"""messages_*: listing, reading, searching and organizing messages."""

import base64
import hashlib
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.server.providers import LocalProvider
from pydantic import Field

from ...domain.errors import ErrorCode, MailError
from ...domain.models import SearchQuery
from ...services import messages
from .base import (
    UNTRUSTED,
    Access,
    AccountParam,
    CursorParam,
    Gateway,
    HandleParam,
    HandlesParam,
    MailboxParam,
    WriteResult,
)

BulkOperation = Literal[tuple(sorted(messages.OPERATIONS))]  # type: ignore[valid-type]
MAX_RAW_BYTES = 10 * 1024 * 1024
FlagNames = Annotated[
    list[str] | None,
    Field(default=None, max_length=20,
          description="Friendly names read, unread, star, unstar, answered, draft, or IMAP "
                      "flags/keywords. Deleting is a separate tool."),
]
DestParam = Annotated[str, Field(min_length=1, max_length=255, description="Destination mailbox.")]


def build(gw: Gateway) -> LocalProvider:
    app, p = gw.app, gw.provider()
    Limit = gw.limit_type()  # noqa: N806 - a type alias

    @gw.tool(p, "messages_list",
             "List message summaries in a mailbox, newest first. Pass next_cursor back as "
             "cursor for the next page. Does not mark messages as read.", title="List messages")
    def messages_list(
        account: AccountParam, mailbox: MailboxParam, limit: Limit = 50,  # type: ignore[valid-type]
        cursor: CursorParam = None,
        unread_only: bool = False,
    ) -> dict[str, Any]:
        return gw.read(lambda c: messages.list_messages(
            app, c, account, mailbox, limit, cursor, unread_only))

    @gw.tool(p, "messages_get",
             "Fetch one message: headers, sanitized body and attachment list. Reading never "
             "marks it read; use messages_set_flags for that.", title="Get message")
    def messages_get(
        handle: HandleParam,
        include_headers: Annotated[bool, Field(description="Include all raw headers.")] = False,
        include_body: bool = True,
        body_format: Literal["text", "html", "both"] = "both",
    ) -> dict[str, Any]:
        def run(c: Any) -> dict[str, Any]:
            msg = messages.get_message(app, c, handle, include_headers, include_body, body_format)
            return {"message": msg, "notice": UNTRUSTED}

        return gw.read(run)

    @gw.tool(p, "messages_get_raw",
             f"Fetch the raw RFC 822 source (base64, at most {MAX_RAW_BYTES // 1024 // 1024} MiB). "
             "Only available when both body and attachment release are permitted.",
             title="Get raw message")
    def messages_get_raw(handle: HandleParam) -> dict[str, Any]:
        def run(c: Any) -> dict[str, Any]:
            raw = messages.get_raw(app, c, handle)
            if len(raw) > MAX_RAW_BYTES:
                raise MailError(ErrorCode.TOO_LARGE, "message is too large to return inline",
                                size=len(raw), limit=MAX_RAW_BYTES)
            return {"handle": handle, "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "content_base64": base64.b64encode(raw).decode("ascii"), "notice": UNTRUSTED}

        return gw.read(run)

    @gw.tool(p, "messages_search",
             "Structured search (sender, recipients, subject, body, dates, size, flags, "
             "headers, any_of/not combinations). Searches INBOX unless mailboxes is given. "
             "Completeness is always reported as unknown.", title="Search messages")
    def messages_search(
        account: AccountParam, query: SearchQuery,
        mailboxes: Annotated[list[str] | None, Field(
            max_length=50, description="Mailboxes to search; default INBOX only.")] = None,
        limit: Limit = 50,  # type: ignore[valid-type]
        cursor: CursorParam = None,
    ) -> dict[str, Any]:
        return gw.read(lambda c: messages.search(app, c, account, query, mailboxes, limit, cursor))

    @gw.tool(p, "messages_bulk_preview",
             "Preview a bulk change without applying it: occurrences per mailbox, a heuristic "
             "unique-message count and the effect plan. Select targets with handles or a query.",
             title="Preview bulk change")
    def messages_bulk_preview(
        account: AccountParam,
        operation: Annotated[BulkOperation, Field(description="The change to preview.")],  # type: ignore[valid-type]
        handles: Annotated[list[str] | None, Field(max_length=1000)] = None,
        query: SearchQuery | None = None,
        mailboxes: Annotated[list[str] | None, Field(max_length=50)] = None,
        dest: str | None = None,
        add: FlagNames = None,
        remove: FlagNames = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 1000,
    ) -> dict[str, Any]:
        return gw.read(lambda c: messages.bulk_preview(
            app, c, account, operation, handles=handles, query=query, mailboxes=mailboxes,
            dest=dest, add=add, remove=remove, limit=limit))

    @gw.tool(p, "messages_set_flags", "Add or remove flags such as read/unread and star/unstar.",
             access=Access.WRITE, idempotent=True, title="Set flags")
    async def messages_set_flags(ctx: Context, handles: HandlesParam, add: FlagNames = None,
                                 remove: FlagNames = None) -> WriteResult:
        return await gw.write(ctx, lambda c: messages.set_flags(app, c, handles, add, remove))

    @gw.tool(p, "messages_move",
             "Move messages to another mailbox. Returns new handles; the old ones become stale.",
             access=Access.WRITE, title="Move messages")
    async def messages_move(ctx: Context, handles: HandlesParam, dest: DestParam) -> WriteResult:
        return await gw.write(ctx, lambda c: messages.move(app, c, handles, dest))

    @gw.tool(p, "messages_archive", "Move messages to Archive.", access=Access.WRITE,
             title="Archive messages")
    async def messages_archive(ctx: Context, handles: HandlesParam) -> WriteResult:
        return await gw.write(ctx, lambda c: messages.archive(app, c, handles))

    @gw.tool(p, "messages_trash", "Move messages to Trash (reversible with messages_restore).",
             access=Access.WRITE, title="Trash messages")
    async def messages_trash(ctx: Context, handles: HandlesParam) -> WriteResult:
        return await gw.write(ctx, lambda c: messages.trash(app, c, handles))

    @gw.tool(p, "messages_restore", "Move messages out of Trash or Spam (default INBOX).",
             access=Access.WRITE, title="Restore messages")
    async def messages_restore(ctx: Context, handles: HandlesParam,
                               dest: Annotated[str | None, Field(max_length=255)] = None
                               ) -> WriteResult:
        return await gw.write(ctx, lambda c: messages.restore(app, c, handles, dest))

    @gw.tool(p, "messages_mark_spam",
             "Move messages to Spam. Does not block the sender or create filters.",
             access=Access.WRITE, title="Mark as spam")
    async def messages_mark_spam(ctx: Context, handles: HandlesParam) -> WriteResult:
        return await gw.write(ctx, lambda c: messages.spam(app, c, handles))

    @gw.tool(p, "messages_not_spam", "Move messages out of Spam (default INBOX).",
             access=Access.WRITE, title="Not spam")
    async def messages_not_spam(ctx: Context, handles: HandlesParam,
                                dest: Annotated[str | None, Field(max_length=255)] = None
                                ) -> WriteResult:
        return await gw.write(ctx, lambda c: messages.not_spam(app, c, handles, dest))

    @gw.tool(p, "messages_mark_deleted",
             "Set the \\Deleted flag. Nothing is removed until messages_expunge or "
             "mailboxes_empty.", access=Access.DESTRUCTIVE, idempotent=True,
             title="Mark deleted")
    async def messages_mark_deleted(ctx: Context, handles: HandlesParam) -> WriteResult:
        return await gw.write(ctx, lambda c: messages.mark_deleted(app, c, handles))

    @gw.tool(p, "messages_clear_deleted", "Clear the \\Deleted flag.", access=Access.WRITE,
             idempotent=True, title="Clear deleted flag")
    async def messages_clear_deleted(ctx: Context, handles: HandlesParam) -> WriteResult:
        return await gw.write(ctx, lambda c: messages.clear_deleted(app, c, handles))

    @gw.tool(p, "messages_expunge",
             "Permanently delete exactly these messages (Trash or Spam only). Other messages "
             "carrying \\Deleted are untouched. Cannot be undone.", access=Access.DESTRUCTIVE,
             idempotent=True, title="Permanently delete messages")
    async def messages_expunge(ctx: Context, handles: HandlesParam) -> WriteResult:
        return await gw.write(ctx, lambda c: messages.expunge(app, c, handles))

    return p


