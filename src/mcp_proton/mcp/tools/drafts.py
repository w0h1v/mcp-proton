"""drafts_*: create, read, edit, discard and send drafts."""

from typing import Annotated, Any

from fastmcp import Context
from fastmcp.server.providers import LocalProvider
from pydantic import BaseModel, ConfigDict, Field

from ...domain.errors import invalid
from ...domain.models import ArtifactAttachment, LocalAttachment, OutgoingMessage
from ...services import drafts
from .base import (
    Access,
    AccountParam,
    AddressInput,
    Gateway,
    HandleParam,
    IdempotencyParam,
    WriteResult,
    addresses,
)

AttachmentItems = Annotated[
    list[LocalAttachment | ArtifactAttachment],
    Field(max_length=50, description="Local files (inside owner-allowed directories) or "
                                     "artifact_ids from attachments_fetch."),
]


class DraftChanges(BaseModel):
    """Fields to change; omitted fields are carried over unchanged."""

    model_config = ConfigDict(extra="forbid")

    to: list[AddressInput] | None = None
    cc: list[AddressInput] | None = None
    bcc: list[AddressInput] | None = None
    reply_to: list[AddressInput] | None = None
    subject: str | None = None
    text: str | None = None
    html: str | None = None
    add_attachments: AttachmentItems | None = None
    remove_attachments: Annotated[list[str] | None, Field(
        max_length=50, description="Part ids of existing attachments to drop.")] = None

    def as_changes(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in self.model_fields_set:
            value = getattr(self, key)
            if key in ("to", "cc", "bcc", "reply_to"):
                out[key] = [a.model_dump() for a in addresses(value)]
            elif key == "add_attachments":
                out[key] = [a.model_dump() for a in value or []]
            else:
                out[key] = value
        if not out:
            raise invalid("no changes given")
        return out


def build(gw: Gateway) -> LocalProvider:
    app, p = gw.app, gw.provider()

    @gw.tool(p, "drafts_create",
             "Save a new draft in Drafts (nothing is sent). Returns the draft's handle.",
             access=Access.WRITE, title="Create draft")
    async def drafts_create(
        ctx: Context, account: AccountParam,
        to: list[AddressInput] | None = None, cc: list[AddressInput] | None = None,
        bcc: list[AddressInput] | None = None, reply_to: list[AddressInput] | None = None,
        subject: str = "", text: str | None = None, html: str | None = None,
        from_address: Annotated[AddressInput | None, Field(
            description="Sender; must be the account address or a configured identity.")] = None,
        in_reply_to: str | None = None, references: list[str] | None = None,
        attachments: AttachmentItems | None = None,
        idempotency_key: IdempotencyParam = None,
    ) -> WriteResult:
        def run(c: Any) -> Any:
            out = OutgoingMessage.model_validate({
                "account": account,
                "from": addresses([from_address])[0] if from_address else None,
                "to": addresses(to), "cc": addresses(cc), "bcc": addresses(bcc),
                "reply_to": addresses(reply_to), "subject": subject, "text": text, "html": html,
                "in_reply_to": in_reply_to, "references": references or [],
                "attachments": attachments or [],
            })
            return drafts.create_draft(app, c, out, idempotency_key)

        return await gw.write(ctx, run)

    @gw.tool(p, "drafts_get", "Read a draft as editable fields with its attachment list.",
             title="Get draft")
    def drafts_get(handle: HandleParam) -> dict[str, Any]:
        return gw.read(lambda c: drafts.get_draft(app, c, handle))

    @gw.tool(p, "drafts_update",
             "Replace a draft with a modified version. Returns the new handle; the old one "
             "becomes stale.", access=Access.WRITE, title="Update draft")
    async def drafts_update(ctx: Context, handle: HandleParam, changes: DraftChanges,
                            idempotency_key: IdempotencyParam = None) -> WriteResult:
        return await gw.write(
            ctx, lambda c: drafts.update_draft(app, c, handle, changes.as_changes(),
                                               idempotency_key))

    @gw.tool(p, "drafts_discard", "Permanently remove one draft.", access=Access.DESTRUCTIVE,
             title="Discard draft")
    async def drafts_discard(ctx: Context, handle: HandleParam,
                             idempotency_key: IdempotencyParam = None) -> WriteResult:
        return await gw.write(ctx, lambda c: drafts.discard_draft(app, c, handle, idempotency_key))

    @gw.tool(p, "drafts_send",
             "Send a stored draft exactly as it is now; the draft is removed afterwards. "
             "Success means the mail server accepted it, not that it was delivered. "
             "delivery_unknown is never retried automatically: use mail_reconcile.",
             access=Access.SEND, title="Send draft")
    async def drafts_send(ctx: Context, handle: HandleParam,
                          idempotency_key: IdempotencyParam = None) -> WriteResult:
        return await gw.write(ctx, lambda c: drafts.send_draft(app, c, handle, idempotency_key))

    return p
