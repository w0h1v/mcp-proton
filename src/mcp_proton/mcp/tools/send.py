"""mail_send, mail_reply, mail_forward, mail_reconcile: outgoing mail."""

from typing import Annotated, Any

from fastmcp import Context
from fastmcp.server.providers import LocalProvider
from pydantic import Field

from ...domain.models import OutgoingMessage
from ...services import sending
from .base import (
    Access,
    AccountParam,
    AddressInput,
    Gateway,
    HandleParam,
    IdempotencyParam,
    OperationIdParam,
    WriteResult,
    addresses,
)
from .drafts import AttachmentItems

SEND_NOTE = (
    " Success means the mail server accepted the message, not that it was delivered. "
    "delivery_unknown is never retried automatically; use mail_reconcile."
)


def build(gw: Gateway) -> LocalProvider:
    app, p = gw.app, gw.provider()

    @gw.tool(p, "mail_send", "Compose and send a new message." + SEND_NOTE,
             access=Access.SEND, title="Send mail")
    async def mail_send(
        ctx: Context, account: AccountParam,
        to: Annotated[list[AddressInput], Field(min_length=1, max_length=100)],
        subject: str, text: str | None = None, html: str | None = None,
        cc: list[AddressInput] | None = None, bcc: list[AddressInput] | None = None,
        reply_to: list[AddressInput] | None = None,
        from_address: Annotated[AddressInput | None, Field(
            description="Sender; must be the account address or a configured identity.")] = None,
        attachments: AttachmentItems | None = None,
        idempotency_key: IdempotencyParam = None,
    ) -> WriteResult:
        def run(c: Any) -> Any:
            out = OutgoingMessage.model_validate({
                "account": account,
                "from": addresses([from_address])[0] if from_address else None,
                "to": addresses(to), "cc": addresses(cc), "bcc": addresses(bcc),
                "reply_to": addresses(reply_to), "subject": subject, "text": text, "html": html,
                "attachments": attachments or [],
            })
            return sending.send(app, c, out, idempotency_key)

        return await gw.write(ctx, run)

    @gw.tool(p, "mail_reply",
             "Reply (or reply-all) to a message; recipients and threading headers come from "
             "the original and cannot be overridden, and your own addresses are never "
             "recipients. quote defaults to true, which copies the original message into the "
             "reply; pass quote=false for short replies." + SEND_NOTE,
             access=Access.SEND, title="Reply")
    async def mail_reply(
        ctx: Context, handle: HandleParam, text: str | None = None, html: str | None = None,
        reply_all: bool = False, quote: Annotated[bool, Field(
            description="Append the quoted original to the text body (default true). Use "
                        "false to avoid copying the original message into the reply.")] = True,
        attachments: AttachmentItems | None = None,
        idempotency_key: IdempotencyParam = None,
    ) -> WriteResult:
        return await gw.write(ctx, lambda c: sending.reply(
            app, c, handle, text, html, reply_all, attachments or [], quote, idempotency_key))

    @gw.tool(p, "mail_forward",
             "Forward a message inline or as an attached .eml." + SEND_NOTE,
             access=Access.SEND, title="Forward")
    async def mail_forward(
        ctx: Context, handle: HandleParam,
        to: Annotated[list[AddressInput], Field(min_length=1, max_length=100)],
        cc: list[AddressInput] | None = None, bcc: list[AddressInput] | None = None,
        text: Annotated[str | None, Field(
            description="Your note above the forwarded text.")] = None,
        as_attachment: bool = False, attachments: AttachmentItems | None = None,
        idempotency_key: IdempotencyParam = None,
    ) -> WriteResult:
        return await gw.write(ctx, lambda c: sending.forward(
            app, c, handle, addresses(to), addresses(cc), addresses(bcc), text, as_attachment,
            attachments or [], idempotency_key))

    @gw.tool(p, "mail_reconcile",
             "Look for the Message-ID of an earlier send in Sent. Evidence only: it never "
             "resends, and absence from Sent does not prove the message was not delivered.",
             title="Reconcile send")
    def mail_reconcile(operation_id: OperationIdParam) -> dict[str, Any]:
        return gw.read(lambda c: sending.reconcile(app, c, operation_id))

    return p
