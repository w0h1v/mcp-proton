import smtplib

from imapclient import IMAPClient


def test_imap_fixture_has_bridge_tree(imap_server):
    c = IMAPClient(imap_server.host, port=imap_server.port, ssl=False)
    c.login(imap_server.user, imap_server.password)
    names = {f[2] for f in c.list_folders()}
    assert {"INBOX", "Labels", "Folders", "Archive", "Spam"} <= names
    c.logout()


def test_smtp_fixture_records_and_rejects(smtp_server):
    smtp_server.reject.add("bad@x.test")
    with smtplib.SMTP(smtp_server.host, smtp_server.port) as s:
        refused = s.sendmail("me@x.test", ["ok@x.test", "bad@x.test"], b"Subject: hi\r\n\r\nbody")
    assert "bad@x.test" in refused
    assert smtp_server.messages[0][1] == ["ok@x.test"]
