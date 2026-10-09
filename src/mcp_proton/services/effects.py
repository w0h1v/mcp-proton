"""Effect table: domain operation x mailbox type -> expected effects and policy family.

Permissions are classified by expected effect, not raw IMAP verb. A label
removal and a permanent deletion can both be ``UID EXPUNGE``; only the source
mailbox type tells them apart. Combinations not listed here return
``unsupported_semantics`` instead of silently escalating impact.

Every entry starts ``verified=False``. An entry becomes verified only through a
live Bridge acceptance case recorded in docs/compatibility.md (operation,
Bridge version, date, settings, observed behaviour). Executors recheck the
mailbox roles at execution time and invalidate plans whose effects changed.

References: https://proton.me/support/labels-in-bridge (label semantics).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ..domain.errors import ErrorCode, MailError
from ..domain.families import OperationFamily as F
from ..domain.models import MailboxRole as R


class Effect(StrEnum):
    ADD_LABEL = "add_label"
    REMOVE_LABEL = "remove_label"
    CHANGE_LOCATION = "change_location"  # exclusive folder location changes
    TRASH = "trash"  # reversible while the message still exists in Trash
    RESTORE = "restore"
    MARK_SPAM = "mark_spam"  # does not block sender or create filters
    UNMARK_SPAM = "unmark_spam"
    FLAG_CHANGE = "flag_change"
    MARK_DELETED = "mark_deleted"  # \Deleted set; destroyed on a later expunge
    DESTROY = "destroy"  # permanent, no undo
    DRAFT_REPLACE = "draft_replace"
    CREATE_MAILBOX = "create_mailbox"
    RENAME_MAILBOX = "rename_mailbox"
    DELETE_FOLDER = "delete_folder"
    DELETE_LABEL = "delete_label"
    APPEND = "append"


class DomainOp(StrEnum):
    MOVE = "move"
    COPY = "copy"
    ARCHIVE = "archive"
    TRASH = "trash"
    RESTORE = "restore"
    SPAM = "spam"
    NOT_SPAM = "not_spam"
    LABEL_ADD = "label_add"
    LABEL_REMOVE = "label_remove"
    FLAGS = "flags"
    MARK_DELETED = "mark_deleted"
    CLEAR_DELETED = "clear_deleted"
    EXPUNGE = "expunge"  # targeted permanent deletion
    EMPTY = "empty"  # empty Trash/Spam from a snapshot
    DRAFT_REPLACE = "draft_replace"
    DRAFT_DISCARD = "draft_discard"
    IMPORT = "import"
    FOLDER_CREATE = "folder_create"
    LABEL_CREATE = "label_create"
    FOLDER_RENAME = "folder_rename"
    LABEL_RENAME = "label_rename"
    FOLDER_DELETE = "folder_delete"
    LABEL_DELETE = "label_delete"


@dataclass(frozen=True)
class EffectPlan:
    op: DomainOp
    effects: tuple[Effect, ...]
    family: F
    also_families: tuple[F, ...] = ()
    reversible: bool = True
    verified: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)


# Mailboxes that are a message's exclusive location in Proton's model.
LOCATIONS = frozenset({R.INBOX, R.FOLDER, R.ARCHIVE, R.SPAM, R.TRASH})
# Views/aggregates whose semantics for mutations are not established.
VIEWS = frozenset({R.ALL_MAIL, R.STARRED, R.SCHEDULED, R.CONTAINER, R.OTHER})


def _unsupported(op: DomainOp, src: R | None, dst: R | None, why: str) -> MailError:
    return MailError(
        ErrorCode.UNSUPPORTED_SEMANTICS,
        f"{op.value} from {src.value if src else '-'} to {dst.value if dst else '-'}: {why}",
        op=op.value,
        source_role=src.value if src else None,
        dest_role=dst.value if dst else None,
    )


def plan(op: DomainOp, source: R | None = None, dest: R | None = None,
         *, non_empty: bool | None = None) -> EffectPlan:
    """Return the expected effects of ``op`` or raise ``unsupported_semantics``."""
    O = DomainOp  # noqa: N806 - local alias for readability
    if op in (O.MOVE, O.ARCHIVE, O.TRASH, O.RESTORE, O.SPAM, O.NOT_SPAM):
        if source not in LOCATIONS:
            raise _unsupported(op, source, dest, "source must be a location mailbox "
                               "(Inbox, Archive, Spam, Trash or a folder); resolve the "
                               "message's location occurrence first")
        if dest not in LOCATIONS:
            raise _unsupported(op, source, dest, "destination must be a location mailbox; "
                               "use labels_apply for labels and flags for Starred")
        if source == dest and source is not R.FOLDER:
            raise _unsupported(op, source, dest, "source and destination are the same mailbox")
        if dest is R.TRASH:
            return EffectPlan(op, (Effect.TRASH,), F.ORGANIZE,
                              notes=("reversible only while the message remains in Trash; "
                                     "provider retention may remove it later",))
        if dest is R.SPAM:
            return EffectPlan(op, (Effect.MARK_SPAM,), F.ORGANIZE,
                              notes=("does not block the sender or create filters",))
        if source is R.SPAM:
            return EffectPlan(op, (Effect.UNMARK_SPAM, Effect.CHANGE_LOCATION), F.ORGANIZE)
        if source is R.TRASH:
            return EffectPlan(op, (Effect.RESTORE, Effect.CHANGE_LOCATION), F.ORGANIZE)
        return EffectPlan(op, (Effect.CHANGE_LOCATION,), F.ORGANIZE,
                          notes=("label memberships are expected to be preserved",))
    if op is O.COPY:
        if dest is R.LABEL:
            return EffectPlan(op, (Effect.ADD_LABEL,), F.ORGANIZE)
        raise _unsupported(op, source, dest, "Proton messages have one location; copying "
                           "into a non-label mailbox has unverified semantics")
    if op is O.LABEL_ADD:
        if dest is not R.LABEL:
            raise _unsupported(op, source, dest, "destination is not a label")
        if source in VIEWS - {R.ALL_MAIL}:
            raise _unsupported(op, source, dest, "source view semantics unverified")
        return EffectPlan(op, (Effect.ADD_LABEL,), F.ORGANIZE)
    if op is O.LABEL_REMOVE:
        if source is not R.LABEL:
            raise _unsupported(op, source, dest, "label removal must target the label "
                               "mailbox occurrence; expunging elsewhere destroys mail")
        return EffectPlan(op, (Effect.REMOVE_LABEL,), F.ORGANIZE,
                          notes=("implemented as targeted UID EXPUNGE in the label mailbox; "
                                 "Proton documents this as label removal",))
    if op is O.FLAGS:
        if source in (R.CONTAINER,):
            raise _unsupported(op, source, dest, "container is not selectable")
        return EffectPlan(op, (Effect.FLAG_CHANGE,), F.ORGANIZE)
    if op is O.MARK_DELETED:
        return EffectPlan(op, (Effect.MARK_DELETED,), F.PERMANENT_DELETE, reversible=True,
                          notes=("a later expunge of this mailbox would destroy the message",))
    if op is O.CLEAR_DELETED:
        return EffectPlan(op, (Effect.FLAG_CHANGE,), F.ORGANIZE)
    if op in (O.EXPUNGE, O.EMPTY):
        if source in (R.TRASH, R.SPAM):
            return EffectPlan(op, (Effect.DESTROY,), F.PERMANENT_DELETE, reversible=False)
        if source is R.LABEL and op is O.EXPUNGE:
            raise _unsupported(op, source, dest, "expunging a label occurrence removes the "
                               "label, not the message; use labels_remove")
        raise _unsupported(op, source, dest, "permanent deletion is only supported from "
                           "Trash or Spam; Bridge behaviour elsewhere depends on mailbox type "
                           "and server settings. Move to Trash first")
    if op is O.DRAFT_REPLACE:
        if source is not R.DRAFTS:
            raise _unsupported(op, source, dest, "not a draft")
        return EffectPlan(op, (Effect.APPEND, Effect.DRAFT_REPLACE), F.DRAFTS)
    if op is O.DRAFT_DISCARD:
        if source is not R.DRAFTS:
            raise _unsupported(op, source, dest, "not a draft")
        return EffectPlan(op, (Effect.DESTROY,), F.DRAFTS, reversible=False,
                          notes=("discarding a draft deletes it permanently",))
    if op is O.IMPORT:
        if dest in VIEWS or dest is None:
            raise _unsupported(op, source, dest, "import target must be a location, label, "
                               "Sent or Drafts mailbox")
        return EffectPlan(op, (Effect.APPEND,), F.IMPORT)
    if op in (O.FOLDER_CREATE, O.LABEL_CREATE):
        return EffectPlan(op, (Effect.CREATE_MAILBOX,), F.ORGANIZE)
    if op is O.FOLDER_RENAME:
        if source is not R.FOLDER:
            raise _unsupported(op, source, dest, "only user folders can be renamed")
        return EffectPlan(op, (Effect.RENAME_MAILBOX,), F.ORGANIZE)
    if op is O.LABEL_RENAME:
        if source is not R.LABEL:
            raise _unsupported(op, source, dest, "only labels can be renamed as labels")
        return EffectPlan(op, (Effect.RENAME_MAILBOX,), F.ORGANIZE)
    if op is O.FOLDER_DELETE:
        if source is not R.FOLDER:
            raise _unsupported(op, source, dest, "system folders cannot be deleted")
        also = (F.PERMANENT_DELETE,) if non_empty is not False else ()
        return EffectPlan(op, (Effect.DELETE_FOLDER,) + ((Effect.DESTROY,) if also else ()),
                          F.FOLDER_DELETE, also_families=also, reversible=False,
                          notes=("effect on contained messages is unverified; treated as "
                                 "possible destruction",) if also else ())
    if op is O.LABEL_DELETE:
        if source is not R.LABEL:
            raise _unsupported(op, source, dest, "not a label")
        return EffectPlan(op, (Effect.DELETE_LABEL, Effect.REMOVE_LABEL), F.LABEL_DELETE,
                          reversible=False,
                          notes=("messages lose this label; their locations are unchanged",))
    raise _unsupported(op, source, dest, "unknown operation")


def effect_table() -> list[dict[str, object]]:
    """Enumerate the table for documentation and the capability report."""
    rows: list[dict[str, object]] = []
    roles = [r for r in R]
    for op in DomainOp:
        for src in [None, *roles]:
            for dst in [None, *roles]:
                try:
                    p = plan(op, src, dst)
                except MailError:
                    continue
                rows.append({"op": op.value, "source": src.value if src else None,
                             "dest": dst.value if dst else None,
                             "effects": [e.value for e in p.effects], "family": p.family.value,
                             "also": [f.value for f in p.also_families],
                             "reversible": p.reversible, "verified": p.verified})
    return rows
