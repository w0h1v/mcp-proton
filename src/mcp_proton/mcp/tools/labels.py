"""labels_*: Proton labels (exposed by Bridge as mailboxes) and label membership."""

from typing import Annotated, Any

from fastmcp import Context
from fastmcp.server.providers import LocalProvider
from pydantic import Field

from ...domain.models import MailboxRole
from ...services import mailboxes, messages
from .base import (
    Access,
    AccountParam,
    Gateway,
    HandlesParam,
    MailboxParam,
    WriteResult,
)


def build(gw: Gateway) -> LocalProvider:
    app, p = gw.app, gw.provider()

    @gw.tool(p, "labels_list", "List labels you may read.", title="List labels")
    def labels_list(account: AccountParam) -> dict[str, Any]:
        def run(c: Any) -> dict[str, Any]:
            items = mailboxes.list_mailboxes(app, c, account)
            return {"items": [m for m in items if m.role is MailboxRole.LABEL]}

        return gw.read(run)

    @gw.tool(p, "labels_create", "Create a label (Labels/<name>).", access=Access.WRITE,
             title="Create label")
    async def labels_create(
        ctx: Context, account: AccountParam,
        name: Annotated[str, Field(min_length=1, max_length=255, description="Label name.")],
    ) -> WriteResult:
        return await gw.write(ctx, lambda c: mailboxes.create_label(app, c, account, name))

    @gw.tool(p, "labels_rename", "Rename a label.", access=Access.WRITE, title="Rename label")
    async def labels_rename(
        ctx: Context, account: AccountParam, mailbox: MailboxParam,
        new_name: Annotated[str, Field(min_length=1, max_length=255)],
    ) -> WriteResult:
        return await gw.write(ctx, lambda c: mailboxes.rename(app, c, account, mailbox, new_name))

    @gw.tool(p, "labels_delete",
             "Delete a label. Messages keep their location and only lose the label.",
             access=Access.DESTRUCTIVE, title="Delete label")
    async def labels_delete(ctx: Context, account: AccountParam,
                            mailbox: MailboxParam) -> WriteResult:
        return await gw.write(ctx, lambda c: mailboxes.delete_label(app, c, account, mailbox))

    @gw.tool(p, "labels_apply",
             "Apply a label to messages (copies them into the label mailbox; other "
             "memberships are untouched).", access=Access.WRITE, title="Apply label")
    async def labels_apply(
        ctx: Context, handles: HandlesParam,
        label: Annotated[str, Field(min_length=1, max_length=255, description="Label name.")],
    ) -> WriteResult:
        return await gw.write(ctx, lambda c: messages.label_apply(app, c, handles, label))

    @gw.tool(p, "labels_remove",
             "Remove labels: pass handles that point into a label mailbox. The message's "
             "other copies (such as INBOX) remain.", access=Access.WRITE, title="Remove label")
    async def labels_remove(ctx: Context, handles: HandlesParam) -> WriteResult:
        return await gw.write(ctx, lambda c: messages.label_remove(app, c, handles))

    return p
