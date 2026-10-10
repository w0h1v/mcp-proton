"""Proton Bridge rejects commands that name a UID that no longer exists.

Observed live (2026-10-09): after ``UID EXPUNGE 1`` succeeded, ``UID SEARCH UID 1``
returned ``NO ... no such message`` instead of RFC 3501's empty result. pymap follows
the RFC, so these tests make it behave like Bridge for explicit UID sets.
"""

from __future__ import annotations

import imaplib

import pytest
from imapclient import IMAPClient

from mcp_proton.bridge.imap import open_store
from mcp_proton.config import AccountConfig, Security
from mcp_proton.domain.errors import ErrorCode, MailError

RAW = (b"From: a@example.com\r\nTo: b@example.com\r\nSubject: s\r\n"
       b"Message-ID: <m@example.com>\r\n\r\nbody\r\n")


@pytest.fixture
def bridge_like(monkeypatch):
    """Reject explicit UID sets naming missing messages, like Bridge does."""
    orig_search, orig_fetch = IMAPClient.search, IMAPClient.fetch

    def present(client):
        return set(orig_search(client, ["ALL"]))

    def search(self, criteria="ALL", charset=None):
        crit = criteria if isinstance(criteria, list) else [criteria]
        if crit and crit[0] == "UID":
            wanted = {int(x) for part in str(crit[1]).split(",") for x in part.split(":")}
            if not wanted <= present(self):
                raise imaplib.IMAP4.error("SEARCH command error: BAD ['no such message']")
        return orig_search(self, criteria, charset)

    def fetch(self, messages, data, modifiers=None):
        if not {int(m) for m in messages} <= present(self):
            raise imaplib.IMAP4.error("FETCH command error: BAD ['no such message']")
        return orig_fetch(self, messages, data, modifiers)

    monkeypatch.setattr(IMAPClient, "search", search)
    monkeypatch.setattr(IMAPClient, "fetch", fetch)


@pytest.fixture
def store(imap_server):
    acct = AccountConfig(name="t", address="demouser@x.test", username=imap_server.user,
                         secret_ref="env:MCP_PROTON_TEST_SECRET", imap_host=imap_server.host,
                         imap_port=imap_server.port, imap_security=Security.NONE)
    s = open_store(acct)
    yield s
    s.close()


def test_expunge_reports_removed_uid_when_server_rejects_missing_uids(store, bridge_like):
    store.create_mailbox("Labels/L")
    r = store.append("Labels/L", RAW)
    store.store_flags("Labels/L", r.uidvalidity, [r.uid], add=["\\Deleted"])
    assert store.expunge_uids("Labels/L", r.uidvalidity, [r.uid]) == [r.uid]


def test_fetch_flags_skips_vanished_uids(store, bridge_like):
    a = store.append("INBOX", RAW)
    b = store.append("INBOX", RAW)
    store.store_flags("INBOX", a.uidvalidity, [b.uid], add=["\\Deleted"])
    store.expunge_uids("INBOX", a.uidvalidity, [b.uid])
    flags = store.fetch_flags("INBOX", a.uidvalidity, [a.uid, b.uid])
    assert set(flags) == {a.uid}


def test_fetch_summaries_and_message_handle_vanished_uids(store, bridge_like):
    a = store.append("INBOX", RAW)
    store.store_flags("INBOX", a.uidvalidity, [a.uid], add=["\\Deleted"])
    store.expunge_uids("INBOX", a.uidvalidity, [a.uid])
    assert store.fetch_summaries("INBOX", a.uidvalidity, [a.uid]) == []
    with pytest.raises(MailError) as e:
        store.fetch_message("INBOX", a.uidvalidity, a.uid)
    assert e.value.code is ErrorCode.NOT_FOUND
    assert "message no longer exists" in e.value.message


def test_no_such_message_is_not_reported_as_missing_mailbox():
    from mcp_proton.bridge.imap import ImapMailStore

    err = ImapMailStore._translate(imaplib.IMAP4.error("NO no such message"), "expunge")
    assert err.code is ErrorCode.NOT_FOUND and "message" in err.message
    err = ImapMailStore._translate(imaplib.IMAP4.error("NO no such mailbox"), "select")
    assert "mailbox" in err.message
