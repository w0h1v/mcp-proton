"""Attachment reads, artifacts, export and filename safety."""

from __future__ import annotations

import base64
import os
import stat
from email.message import EmailMessage

import pytest

from mcp_proton.domain.errors import ErrorCode, MailError
from mcp_proton.domain.models import ArtifactAttachment, MessageHandle, OperationStatus
from mcp_proton.policy.model import Constraints, Preset
from mcp_proton.services import attachments as att

from .conftest import ACCOUNT, ADDRESS

PDF = b"%PDF-1.4 fake attachment bytes"


def put(app, filename="report.pdf", data=PDF, mailbox="INBOX") -> str:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "a@example.com", ADDRESS, "has attachment"
    m["Message-ID"] = f"<{abs(hash((filename, data)))}@example.com>"
    m.set_content("see attached")
    m.add_attachment(data, maintype="application", subtype="pdf", filename=filename)
    r = app.store(ACCOUNT).append(mailbox, m.as_bytes())
    return MessageHandle(account=ACCOUNT, mailbox=mailbox, uidvalidity=r.uidvalidity,
                         uid=r.uid).token()


def constraints(tmp_path, **kw) -> Constraints:
    return Constraints(attachment_ingest_dirs=[str(tmp_path / "ingest")],
                       export_dirs=[str(tmp_path / "export")],
                       import_dirs=[str(tmp_path / "import")], **kw)


def test_list_and_get_attachment(app, caller):
    h = put(app)
    [info] = att.list_attachments(app, caller, h)
    assert info.filename == "report.pdf" and info.part_id == "2" and info.size == len(PDF)
    got = att.get_attachment(app, caller, h, "2")
    assert base64.b64decode(got["content_base64"]) == PDF
    assert got["content_type"] == "application/pdf" and got["size"] == len(PDF)
    with pytest.raises(MailError) as e:
        att.get_attachment(app, caller, h, "9")
    assert e.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(MailError):
        att.get_attachment(app, caller, h, "../1")


def test_get_attachment_too_large_hints_export(app, caller):
    h = put(app)
    with pytest.raises(MailError) as e:
        att.get_attachment(app, caller, h, "2", max_bytes=5)
    assert e.value.code is ErrorCode.TOO_LARGE and e.value.details["hint"] == "export"
    assert "export" in e.value.message


def test_owner_size_limit_caps_requested_max(make_app, caller, tmp_path):
    app = make_app(constraints=constraints(tmp_path, max_attachment_bytes=8))
    h = put(app)
    with pytest.raises(MailError) as e:
        att.get_attachment(app, caller, h, "2", max_bytes=10_000_000)
    assert e.value.code is ErrorCode.TOO_LARGE


def test_release_attachments_restriction(make_app, caller, tmp_path):
    app = make_app(constraints=constraints(tmp_path, release_attachments=False))
    h = put(app)
    assert att.list_attachments(app, caller, h)  # metadata is still visible
    for call in (lambda: att.get_attachment(app, caller, h, "2"),
                 lambda: att.fetch_to_artifact(app, caller, h, "2"),
                 lambda: att.export(app, caller, h, "2", str(tmp_path / "export"))):
        with pytest.raises(MailError) as e:
            call()
        assert e.value.code is ErrorCode.CONSTRAINT_VIOLATION


def test_reads_pass_read_policy(make_app, caller, tmp_path):
    app = make_app(constraints=Constraints(allowed_mailboxes={ACCOUNT: ["INBOX"]}))
    h = put(app, mailbox="Archive")
    with pytest.raises(MailError) as e:
        att.list_attachments(app, caller, h)
    assert e.value.code is ErrorCode.CONSTRAINT_VIOLATION


def test_fetch_to_artifact_storage(app, caller):
    h = put(app)
    art = att.fetch_to_artifact(app, caller, h, "2")
    assert art["artifact_id"].startswith("art_") and art["filename"] == "report.pdf"
    d = app.config.resolved_artifact_dir()
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    f = d / art["artifact_id"]
    assert f.read_bytes() == PDF and stat.S_IMODE(f.stat().st_mode) == 0o600
    row = app.db.query("SELECT * FROM artifacts WHERE id=?", (art["artifact_id"],))[0]
    assert row["source_handle"] == h and row["part_id"] == "2" and row["size"] == len(PDF)


def test_artifact_manifest_detects_tampering_and_expiry(app, caller):
    h = put(app)
    art = att.fetch_to_artifact(app, caller, h, "2")
    items = [ArtifactAttachment(artifact_id=art["artifact_id"])]
    manifest = att.build_manifest(app, caller, items)
    assert manifest[0]["sha256"] and att.load_manifest(app, manifest)[0].data == PDF
    (app.config.resolved_artifact_dir() / art["artifact_id"]).write_bytes(b"tampered!")
    with pytest.raises(MailError) as e:
        att.load_manifest(app, manifest)
    assert e.value.code is ErrorCode.CONFLICT
    with pytest.raises(MailError) as e2:
        att.resolve_attachments(app, caller, [ArtifactAttachment(artifact_id="art_nope")])
    assert e2.value.code is ErrorCode.NOT_FOUND
    app.db.query("SELECT 1")
    with app.db.tx() as c:
        c.execute("UPDATE artifacts SET expires_at='2000-01-01T00:00:00+00:00'")
    with pytest.raises(MailError):
        att.build_manifest(app, caller, items)
    assert att.purge_expired_artifacts(app) == 1
    assert not (app.config.resolved_artifact_dir() / art["artifact_id"]).exists()


def test_export_writes_into_export_dir(app, caller, tmp_path):
    h = put(app)
    dest = tmp_path / "export" / "sub"
    o = att.export(app, caller, h, "2", str(dest))
    assert o.status is OperationStatus.SUCCEEDED
    assert (dest / "report.pdf").read_bytes() == PDF
    assert stat.S_IMODE((dest / "report.pdf").stat().st_mode) == 0o600
    assert o.result["filename"] == "report.pdf" and "path" in o.result


def test_export_never_overwrites(app, caller, tmp_path):
    h = put(app)
    dest = tmp_path / "export"
    (dest / "report.pdf").write_bytes(b"precious")
    o1 = att.export(app, caller, h, "2", str(dest))
    o2 = att.export(app, caller, h, "2", str(dest))
    assert (dest / "report.pdf").read_bytes() == b"precious"
    assert o1.result["filename"] == "report (1).pdf" and o2.result["filename"] == "report (2).pdf"


def test_export_does_not_follow_symlink_at_target(app, caller, tmp_path):
    h = put(app)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    os.symlink(victim, tmp_path / "export" / "report.pdf")
    o = att.export(app, caller, h, "2", str(tmp_path / "export"))
    assert victim.read_text() == "keep" and o.result["filename"] == "report (1).pdf"


@pytest.mark.parametrize("name", ["../evil.pdf", "a/b.pdf", "..", ".", "a\\b", "x\x00.pdf",
                                  "bad\nname", "CON", "nul.txt", ""])
def test_export_rejects_bad_requested_filename(app, caller, tmp_path, name):
    h = put(app)
    with pytest.raises(MailError) as e:
        att.export(app, caller, h, "2", str(tmp_path / "export"), filename=name)
    assert e.value.code is ErrorCode.PATH_REJECTED
    assert os.listdir(tmp_path / "export") == []


def test_export_custom_filename(app, caller, tmp_path):
    h = put(app)
    o = att.export(app, caller, h, "2", str(tmp_path / "export"), filename="mine.bin")
    assert o.result["filename"] == "mine.bin"


def test_export_dest_outside_export_dirs_rejected(app, caller, tmp_path):
    h = put(app)
    for bad in (tmp_path / "elsewhere", tmp_path / "export" / ".." / "elsewhere", "/etc"):
        with pytest.raises(MailError) as e:
            att.export(app, caller, h, "2", str(bad))
        assert e.value.code is ErrorCode.PATH_REJECTED
    assert not (tmp_path / "elsewhere").exists()


def test_export_symlinked_dir_escape_rejected(app, caller, tmp_path):
    h = put(app)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, tmp_path / "export" / "link")
    with pytest.raises(MailError) as e:
        att.export(app, caller, h, "2", str(tmp_path / "export" / "link"))
    assert e.value.code is ErrorCode.PATH_REJECTED
    assert os.listdir(outside) == []


def test_hostile_message_filename_is_sanitized_on_export(app, caller, tmp_path):
    h = put(app, filename="../../etc/cron.d/evil")
    o = att.export(app, caller, h, "2", str(tmp_path / "export"))
    assert o.status is OperationStatus.SUCCEEDED
    assert o.result["filename"] == "evil"
    assert os.listdir(tmp_path / "export") == ["evil"]


def test_export_policy_families(make_app, caller, owner, tmp_path):
    reader = make_app(Preset.READER, constraints=constraints(tmp_path))
    h = put(reader)
    with pytest.raises(MailError) as e:
        att.export(reader, caller, h, "2", str(tmp_path / "export"))
    assert e.value.code is ErrorCode.POLICY_DENIED
    asst = make_app(Preset.ASSISTANT, constraints=constraints(tmp_path))
    h2 = put(asst)
    o = att.export(asst, caller, h2, "2", str(tmp_path / "export"))
    assert o.status is OperationStatus.PENDING
    assert not (tmp_path / "export" / "report.pdf").exists()
    done = asst.approve(owner, o.operation_id, True, execute=True)
    assert done.status is OperationStatus.SUCCEEDED
    assert (tmp_path / "export" / "report.pdf").exists()


@pytest.mark.parametrize("raw,expected", [
    ("report.pdf", "report.pdf"),
    ("../../x.sh", "x.sh"),
    ("C:\\Users\\a\\b.txt", "b.txt"),
    ("a\x00b\x1f.txt", "a_b_.txt"),
    ("  ..hidden ", "hidden"),
    ("CON", "_CON"),
    ("nul.txt", "_nul.txt"),
    ("", "attachment"),
    ("...", "attachment"),
    ("trailing. ", "trailing"),
])
def test_sanitize_filename(raw, expected):
    assert att.sanitize_filename(raw) == expected


def test_sanitize_filename_truncates_but_keeps_extension():
    name = att.sanitize_filename("é" * 300 + ".pdf")
    assert name.endswith(".pdf") and len(name.encode()) <= 200


def test_validate_filename_accepts_plain_names():
    assert att.validate_filename("my file (1).txt") == "my file (1).txt"
