"""Operation families: the unit of policy.

Permissions are classified by *expected effect*, not by raw IMAP verb. Each
operation kind (e.g. ``messages.move``) maps to exactly one family; the policy
engine decides Allow/Ask/Deny per family. Families absent from a preset are
unavailable (Deny) until an owner explicitly configures them.
"""

from __future__ import annotations

from enum import StrEnum


class OperationFamily(StrEnum):
    # Columns of the preset table
    READ = "read"
    ORGANIZE = "organize"  # flags, filing, labels apply/remove, folder/label create+rename
    DRAFTS = "drafts"
    SEND = "send"
    PERMANENT_DELETE = "permanent_delete"
    # Separately configured families
    ATTACHMENT_INGEST = "attachment_ingest"  # local files -> drafts/outgoing
    EXPORT = "export"  # attachments/messages -> local filesystem
    IMPORT = "import"  # local EML/mbox -> mailbox
    FOLDER_DELETE = "folder_delete"
    LABEL_DELETE = "label_delete"
    AUTOMATION = "automation"  # schedules, reminders, rules, webhooks


# Families grouped by the preset table column they summarize.
PRESET_COLUMNS: dict[str, tuple[OperationFamily, ...]] = {
    "reading": (OperationFamily.READ,),
    "organizing_and_drafts": (OperationFamily.ORGANIZE, OperationFamily.DRAFTS),
    "sending": (OperationFamily.SEND,),
    "permanent_deletion": (OperationFamily.PERMANENT_DELETE,),
    "additional": (
        OperationFamily.ATTACHMENT_INGEST,
        OperationFamily.EXPORT,
        OperationFamily.IMPORT,
        OperationFamily.FOLDER_DELETE,
        OperationFamily.LABEL_DELETE,
        OperationFamily.AUTOMATION,
    ),
}


# Registry of operation kinds -> family. Service modules register their kinds
# here; an operation kind that is not registered cannot be executed.
OPERATION_KINDS: dict[str, OperationFamily] = {}


def register_kind(kind: str, family: OperationFamily) -> str:
    existing = OPERATION_KINDS.get(kind)
    if existing is not None and existing != family:
        raise ValueError(f"operation kind {kind!r} already registered as {existing}")
    OPERATION_KINDS[kind] = family
    return kind


def family_of(kind: str) -> OperationFamily | None:
    return OPERATION_KINDS.get(kind)
