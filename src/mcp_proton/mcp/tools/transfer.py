"""imports_* and exports_*: EML/mbox import and local-file export."""

from typing import Annotated, Literal

from fastmcp import Context
from fastmcp.server.providers import LocalProvider
from pydantic import Field

from ...services import attachments, transfer
from .base import (
    Access,
    AccountParam,
    Gateway,
    HandleParam,
    HandlesParam,
    IdempotencyParam,
    MailboxParam,
    WriteResult,
)

FlagList = Annotated[list[str] | None, Field(max_length=20, description="Flags to set on import.")]
PathParam = Annotated[str, Field(min_length=1, max_length=4096,
                                 description="Absolute path inside an owner-allowed directory.")]


def build(gw: Gateway) -> LocalProvider:
    app, p = gw.app, gw.provider()

    @gw.tool(p, "imports_eml",
             "Import .eml files (or directories of them) into a mailbox. Paths must be inside "
             "owner-allowed import directories. Resume an interrupted import with the "
             "manifest id from its result.", access=Access.WRITE, title="Import EML files")
    async def imports_eml(
        ctx: Context, account: AccountParam,
        paths: Annotated[list[PathParam], Field(min_length=1, max_length=1000)],
        mailbox: MailboxParam, preserve_date: bool = True, flags: FlagList = None,
        skip_duplicates: bool = True,
        resume_manifest: Annotated[str | None, Field(max_length=128)] = None,
        idempotency_key: IdempotencyParam = None,
    ) -> WriteResult:
        return await gw.write(ctx, lambda c: transfer.import_eml(
            app, c, account, paths, mailbox, preserve_date, flags, skip_duplicates,
            resume_manifest, idempotency_key))

    @gw.tool(p, "imports_mbox", "Import every message of one mbox file into a mailbox.",
             access=Access.WRITE, title="Import mbox")
    async def imports_mbox(
        ctx: Context, account: AccountParam, path: PathParam, mailbox: MailboxParam,
        preserve_date: bool = True, flags: FlagList = None, skip_duplicates: bool = True,
        resume_manifest: Annotated[str | None, Field(max_length=128)] = None,
        idempotency_key: IdempotencyParam = None,
    ) -> WriteResult:
        return await gw.write(ctx, lambda c: transfer.import_mbox(
            app, c, account, path, mailbox, preserve_date, flags, skip_duplicates,
            resume_manifest, idempotency_key))

    @gw.tool(p, "exports_messages",
             "Write raw messages to an owner-allowed export directory as .eml files or one mbox.",
             access=Access.WRITE, title="Export messages")
    async def exports_messages(
        ctx: Context, handles: HandlesParam, dest_dir: PathParam,
        format: Literal["eml", "mbox"] = "eml",  # noqa: A002 - parameter name is the API
        idempotency_key: IdempotencyParam = None,
    ) -> WriteResult:
        return await gw.write(ctx, lambda c: transfer.export_messages(
            app, c, handles, dest_dir, format, idempotency_key))

    @gw.tool(p, "exports_attachment",
             "Write one attachment into an owner-allowed export directory (never overwrites).",
             access=Access.WRITE, title="Export attachment")
    async def exports_attachment(
        ctx: Context, handle: HandleParam,
        part_id: Annotated[str, Field(min_length=1, max_length=64)], dest_dir: PathParam,
        filename: Annotated[str | None, Field(
            max_length=255, description="Plain file name, no directories.")] = None,
    ) -> WriteResult:
        return await gw.write(ctx, lambda c: attachments.export(
            app, c, handle, part_id, dest_dir, filename))

    return p
