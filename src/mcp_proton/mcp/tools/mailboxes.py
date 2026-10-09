"""mailboxes_*: folders and mailbox metadata."""

from typing import Annotated, Any

from fastmcp import Context
from fastmcp.server.providers import LocalProvider
from pydantic import Field

from ...services import mailboxes
from .base import (
    Access,
    AccountParam,
    Gateway,
    MailboxParam,
    WriteResult,
)


def build(gw: Gateway) -> LocalProvider:
    app, p = gw.app, gw.provider()

    @gw.tool(p, "mailboxes_list",
             "List mailboxes (folders, labels and system folders) you may read. Set "
             "with_counts for message and unread totals (slower).", title="List mailboxes")
    def mailboxes_list(
        account: AccountParam,
        with_counts: Annotated[bool, Field(description="Include message/unread counts.")] = False,
    ) -> dict[str, Any]:
        return gw.read(lambda c: {"items": mailboxes.list_mailboxes(app, c, account, with_counts)})

    @gw.tool(p, "mailboxes_status", "Counts, UIDVALIDITY and flags for one mailbox.",
             title="Mailbox status")
    def mailboxes_status(account: AccountParam, mailbox: MailboxParam) -> dict[str, Any]:
        return gw.read(lambda c: mailboxes.status(app, c, account, mailbox))

    @gw.tool(p, "mailboxes_create_folder",
             "Create a folder (Folders/<parent>/<name>). Creating an existing folder is an error.",
             access=Access.WRITE, title="Create folder")
    async def mailboxes_create_folder(
        ctx: Context, account: AccountParam,
        name: Annotated[str, Field(min_length=1, max_length=255, description="Folder name.")],
        parent: Annotated[str | None, Field(description="Existing parent folder path.")] = None,
    ) -> WriteResult:
        return await gw.write(ctx, lambda c: mailboxes.create_folder(app, c, account, name, parent))

    @gw.tool(p, "mailboxes_rename",
             "Rename a user folder or label within its own namespace.",
             access=Access.WRITE, title="Rename mailbox")
    async def mailboxes_rename(
        ctx: Context, account: AccountParam, mailbox: MailboxParam,
        new_name: Annotated[str, Field(min_length=1, max_length=255)],
    ) -> WriteResult:
        return await gw.write(ctx, lambda c: mailboxes.rename(app, c, account, mailbox, new_name))

    @gw.tool(p, "mailboxes_delete_folder",
             "Delete a user folder. System folders are refused; a folder holding messages also "
             "needs permanent-delete permission.", access=Access.DESTRUCTIVE,
             title="Delete folder")
    async def mailboxes_delete_folder(ctx: Context, account: AccountParam,
                                      mailbox: MailboxParam) -> WriteResult:
        return await gw.write(ctx, lambda c: mailboxes.delete_folder(app, c, account, mailbox))

    @gw.tool(p, "mailboxes_subscribe",
             "Set the IMAP subscription state of a mailbox (organizational only).",
             access=Access.WRITE, idempotent=True, title="Subscribe mailbox")
    async def mailboxes_subscribe(
        ctx: Context, account: AccountParam, mailbox: MailboxParam,
        subscribed: Annotated[bool, Field(description="True to subscribe, false to unsubscribe.")],
    ) -> WriteResult:
        return await gw.write(
            ctx, lambda c: mailboxes.subscribe(app, c, account, mailbox, subscribed))

    @gw.tool(p, "mailboxes_empty",
             "Permanently delete the messages currently in Trash or Spam. The message list is "
             "snapshotted when you call this; later arrivals are not deleted.",
             access=Access.DESTRUCTIVE, title="Empty Trash or Spam")
    async def mailboxes_empty(ctx: Context, account: AccountParam,
                              mailbox: MailboxParam) -> WriteResult:
        return await gw.write(ctx, lambda c: mailboxes.empty(app, c, account, mailbox))

    return p
