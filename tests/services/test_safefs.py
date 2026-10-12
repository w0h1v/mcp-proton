"""Symlink swaps after the path check must not escape the configured roots
(security review finding: check-then-open on a resolved path string)."""

from __future__ import annotations

import os

import pytest

from mcp_proton.domain.errors import ErrorCode, MailError
from mcp_proton.domain.families import OperationFamily as F
from mcp_proton.domain.models import LocalAttachment
from mcp_proton.services import attachments as att
from mcp_proton.services import safefs

from .conftest import ACCOUNT

pytestmark = pytest.mark.skipif(not safefs.SUPPORTED, reason="needs dir_fd and O_NOFOLLOW")


@pytest.fixture
def outside(tmp_path):
    d = tmp_path / "outside"
    d.mkdir()
    (d / "f.txt").write_bytes(b"service secret")
    return d


def _swap_to_link(directory, target):
    """Replace ``directory`` with a symlink to ``target`` (what a client with write
    access to the shared root would do between check and use)."""
    for child in directory.iterdir():
        child.unlink()
    directory.rmdir()
    directory.symlink_to(target, target_is_directory=True)


def test_open_file_refuses_a_component_swapped_after_the_check(app, tmp_path, outside,
                                                              monkeypatch):
    sub = tmp_path / "ingest" / "sub"
    sub.mkdir()
    (sub / "f.txt").write_bytes(b"ok")
    sets = safefs.root_sets(app, "hermes", F.ATTACHMENT_INGEST)
    checked = safefs._anchor(str(sub / "f.txt"), sets)  # the check, before the swap
    _swap_to_link(sub, outside)
    monkeypatch.setattr(safefs, "_anchor", lambda path, s: checked)
    with pytest.raises(MailError) as e:
        safefs.open_file(app, "hermes", F.ATTACHMENT_INGEST, str(sub / "f.txt"))
    assert e.value.code is ErrorCode.PATH_REJECTED


def test_open_dir_refuses_a_component_swapped_after_the_check(app, tmp_path, outside,
                                                             monkeypatch):
    sub = tmp_path / "export" / "sub"
    sub.mkdir()
    sets = safefs.root_sets(app, "hermes", F.EXPORT)
    checked = safefs._anchor(str(sub / "deeper"), sets)
    _swap_to_link(sub, outside)
    monkeypatch.setattr(safefs, "_anchor", lambda path, s: checked)
    with pytest.raises(MailError) as e, \
            safefs.open_dir(app, "hermes", F.EXPORT, str(sub / "deeper")):
        pass
    assert e.value.code is ErrorCode.PATH_REJECTED
    assert sorted(p.name for p in outside.iterdir()) == ["f.txt"]  # nothing created there


def test_attachment_ingest_swap_between_check_and_read(app, caller, tmp_path, outside,
                                                       monkeypatch):
    sub = tmp_path / "ingest" / "sub"
    sub.mkdir()
    (sub / "f.txt").write_bytes(b"ok")
    real_require = app.require_path

    def require_then_swap(c, family, path):
        real_require(c, family, path)
        _swap_to_link(sub, outside)

    monkeypatch.setattr(app, "require_path", require_then_swap)
    with pytest.raises(MailError) as e:
        att.build_manifest(app, caller, [LocalAttachment(path=str(sub / "f.txt"))], ACCOUNT)
    assert e.value.code is ErrorCode.PATH_REJECTED


def test_symlinked_file_inside_the_root_is_not_followed(app, tmp_path, outside):
    link = tmp_path / "import" / "link.eml"
    os.symlink(outside / "f.txt", link)
    with pytest.raises(MailError) as e:
        safefs.open_file(app, "hermes", F.IMPORT, str(link))
    assert e.value.code is ErrorCode.PATH_REJECTED  # resolves outside the root


def test_client_root_narrower_than_global_root_is_the_anchor(make_app, tmp_path):
    from mcp_proton.policy.model import ClientConfig, Constraints

    shared = tmp_path / "shared"
    (shared / "a").mkdir(parents=True)
    (shared / "a" / "f.eml").write_bytes(b"x")
    app = make_app(constraints=Constraints(import_dirs=[str(shared)]),
                   clients=[ClientConfig(client_id="hermes", constraints=Constraints(
                       import_dirs=[str(shared / "a")]))])
    root, parts = safefs._anchor(str(shared / "a" / "f.eml"),
                                 safefs.root_sets(app, "hermes", F.IMPORT))
    assert root == os.path.realpath(shared / "a") and parts == ["f.eml"]
    with safefs.open_file(app, "hermes", F.IMPORT, str(shared / "a" / "f.eml")) as fh:
        assert fh.read() == b"x"
