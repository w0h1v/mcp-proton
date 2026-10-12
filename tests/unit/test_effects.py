import pytest

from mcp_proton.domain.errors import ErrorCode, MailError
from mcp_proton.domain.families import OperationFamily as F
from mcp_proton.domain.models import MailboxRole as R
from mcp_proton.services.effects import DomainOp as O
from mcp_proton.services.effects import Effect, effect_table, plan


def test_label_removal_vs_destruction_share_verb_but_not_family():
    assert plan(O.LABEL_REMOVE, R.LABEL).family is F.ORGANIZE
    assert plan(O.EXPUNGE, R.TRASH).family is F.PERMANENT_DELETE
    with pytest.raises(MailError) as e:
        plan(O.EXPUNGE, R.LABEL)
    assert e.value.code is ErrorCode.UNSUPPORTED_SEMANTICS


def test_expunge_outside_trash_is_unsupported():
    with pytest.raises(MailError):
        plan(O.EXPUNGE, R.INBOX)


def test_moves():
    assert plan(O.TRASH, R.INBOX, R.TRASH).effects == (Effect.TRASH,)
    assert plan(O.MOVE, R.FOLDER, R.FOLDER).effects == (Effect.CHANGE_LOCATION,)
    with pytest.raises(MailError):
        plan(O.MOVE, R.LABEL, R.INBOX)
    with pytest.raises(MailError):
        plan(O.MOVE, R.INBOX, R.LABEL)
    with pytest.raises(MailError):
        plan(O.MOVE, R.ALL_MAIL, R.ARCHIVE)


def test_folder_delete_requires_permanent_delete_when_non_empty():
    assert plan(O.FOLDER_DELETE, R.FOLDER, non_empty=True).also_families == (F.PERMANENT_DELETE,)
    assert plan(O.FOLDER_DELETE, R.FOLDER, non_empty=False).also_families == ()
    with pytest.raises(MailError):
        plan(O.FOLDER_DELETE, R.INBOX)


def test_table_is_unverified():
    rows = effect_table()
    assert rows and all(r["verified"] is False for r in rows)


def test_every_destroying_plan_needs_permanent_delete():
    """A plan that can destroy a message must pass the permanent-delete policy,
    whatever its own family (found by the security review: draft discard did not)."""
    import itertools

    roles = [None, *R]
    checked = 0
    for op, src, dst, non_empty in itertools.product(O, roles, roles,
                                                     (None, True, False)):
        try:
            p = plan(op, src, dst, non_empty=non_empty)
        except MailError:
            continue
        if Effect.DESTROY in p.effects:
            checked += 1
            assert F.PERMANENT_DELETE in (p.family, *p.also_families), p
    assert checked
