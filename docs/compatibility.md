# Bridge compatibility matrix

This matrix records which Proton Mail Bridge operations mcp-proton has been tested against, and on which Bridge version.

Every row is `implemented, unverified` or `not implemented`, because the packaged evidence file is empty: no probe report has been recorded into it. Live runs on Bridge 03.27.01 are described under [Observed Bridge behaviour](#observed-bridge-behaviour); record your own with `mcp-proton probe` (see [Recording evidence](#recording-evidence)). Fake-server tests do not count as compatibility evidence.

The rows match the inventory in `src/mcp_proton/services/inventory.py`. The runtime status reported by `mcp-proton diagnostics` comes from the same inventory and the evidence file. The status values there are `available`, `unavailable` and `unverified`.

## Matrix

| operation | Bridge version | test date | configuration | capability prerequisite | implementation status | test evidence | known limits |
|---|---|---|---|---|---|---|---|
| accounts, health, reconnect, diagnostics | — | — | — | none | implemented, unverified | fake-server tests only | none recorded |
| list hierarchy, special folders, counts | — | — | — | none | implemented, unverified | fake-server tests only | none recorded |
| subscriptions | — | — | — | none | implemented, unverified | fake-server tests only | Bridge subscription behaviour unverified |
| paged listing, full message, headers, body, raw | — | — | — | none | implemented, unverified | fake-server tests only | reads use BODY.PEEK; marking read is explicit |
| structured search translated to IMAP SEARCH | — | — | — | none | implemented, unverified | fake-server tests only | completeness reported as unknown while Bridge sync state cannot be established |
| metadata, bytes, export, attach local files, inline CID | — | — | — | none | implemented, unverified | fake-server tests only | none recorded |
| read/unread, star/unstar, other permanent flags | — | — | — | none | implemented, unverified | fake-server tests only | custom keywords are not Proton labels |
| move, archive, trash, restore, spam, not-spam | — | — | — | MOVE | implemented, unverified | fake-server tests only | moving to Spam does not block senders or create filters |
| list, create, apply, remove (labels) | — | — | — | UIDPLUS | implemented, unverified | fake-server tests only | none recorded |
| rename, delete (labels) | — | — | — | none | implemented, unverified | fake-server tests only | none recorded |
| create nested folders | — | — | — | none | implemented, unverified | fake-server tests only | none recorded |
| rename/move hierarchy, delete (folders) | — | — | — | none | implemented, unverified | fake-server tests only | none recorded |
| create, read, update, discard, send (drafts) | — | — | — | UIDPLUS | implemented, unverified | fake-server tests only | none recorded |
| compose, reply, reply-all, forward inline/attachment | — | — | — | none | implemented, unverified | fake-server tests only | success means server acceptance, not recipient delivery; exactly-once delivery is not promised |
| mark/clear deleted, targeted expunge, empty trash/spam | — | — | — | UIDPLUS | implemented, unverified | fake-server tests only | none recorded |
| bulk mutations with preview, progress, partial results | — | — | — | none | implemented, unverified | fake-server tests only | none recorded |
| EML and mbox import/export with manifests | — | — | — | none | implemented, unverified | fake-server tests only | no cross-account move atomicity |
| new mail, flags, memberships, deletions | — | — | — | none | implemented, unverified | fake-server tests only | IDLE watches one mailbox per connection; others are polled |
| Message-ID/References grouping (presentation only) | — | — | — | none | implemented, unverified | fake-server tests only | does not promise exact Proton conversation matching |
| CONDSTORE/QRESYNC, SORT/THREAD, QUOTA, NAMESPACE | — | — | — | none | not implemented | none | detected and reported; core behaviour does not depend on them |
| native scheduled send, contacts, calendar, server filters, address provisioning, key administration | — | — | — | none | not implemented (outside Proton Mail Bridge, not a Bridge operation) | none | outside Proton Mail Bridge |

Two rows differ from the usual `implemented, unverified` status. The extended-protocol row and the out-of-Bridge row are not implemented in the inventory (`implemented=False`), so they carry no evidence.

## Recording evidence

Evidence comes from a live probe against a **dedicated test account**. The probe creates and deletes its own `Folders/mcp-proton-probe-*` and `Labels/mcp-proton-probe-*-label` mailboxes and synthetic messages. It does not touch existing messages.

1. Run the probe and write the report to a file. The command requires an explicit confirmation flag:

   ```sh
   mcp-proton probe <test account> --yes-dedicated-test-account --out probe-report.json
   ```

   Add `--send-to <address you control>` to probe SMTP and Sent filing.

2. Review the report by hand. The probe records what was observed. It does not decide what Bridge should do.

3. Merge the report you reviewed into the evidence file. This does not connect to Bridge or rerun the probe:

   ```sh
   mcp-proton probe <test account> --record-report probe-report.json --reviewed-by "<reviewer>"
   ```

   `--record` on a probe run records that run's results without a separate review step.

Each recorded observation adds the Bridge version to `versions` for its operation, and adds an entry to `evidence`:

```json
{
  "schema": 1,
  "operations": {
    "<operation text, exactly as in the matrix>": {
      "versions": ["<Bridge version>"],
      "evidence": [
        {
          "probe": "<probe name>",
          "bridge_version": "<Bridge version>",
          "date": "<ISO timestamp>",
          "configuration": {},
          "observed": {},
          "reviewed_by": "<reviewer>"
        }
      ]
    }
  }
}
```

The inventory marks an operation `available` only when the connected Bridge version appears in `versions` and the operation's prerequisites are advertised. Fake-server results never create evidence.

Evidence is read from two places and merged: the packaged `src/mcp_proton/compatibility.json` (evidence published with a release) and `compatibility.json` in the mcp-proton configuration directory (evidence the owner recorded locally with `mcp-proton probe --record`). The packaged file is currently empty.

Do not record message contents, addresses or credentials in the evidence file. Review the `observed` fields before you commit them.

## Observed Bridge behaviour

Findings from live runs. They are not evidence records (those come from `mcp-proton probe --record`), but they changed the adapter.

| Date | Observation | Consequence |
|---|---|---|
| 2026-10-09 | After a successful `UID EXPUNGE 1` in a label mailbox, `UID SEARCH UID 1` returned `NO ... no such message` instead of an empty result (RFC 3501). Expunge in the label mailbox removed the label (Proton API returned 200). | The adapter never names possibly-missing UIDs in a search; it lists `UID SEARCH ALL` and intersects. Reads retry once with present UIDs when Bridge rejects a missing one. "no such message" is reported as a missing message, not a missing mailbox. |
| 2026-10-09 | An unpaced full probe run was followed by a Bridge sync error (UserBadEvent). A paced rerun (stop on first error) did not reproduce it. Cause not established. | The probe pauses between steps by default (`--pace`, 2 s) and supports `--stop-on-error`. |
| 2026-10-10 | Bridge 03.27.01 rejected creating a label whose name matched an existing folder: HTTP 409, code 2500, "Label or folder with this name already exists". The IMAP `CREATE` failed with a conflict. Folders and labels share one name namespace. | Probe labels end in `-label`, so every probe mailbox has a distinct name. A setup failure is now recorded in the report (`stopped_at: setup`, `created_mailboxes`) and the probe exits 1 instead of aborting without a report. |
| 2026-10-10 | Full probe run, Bridge 03.27.01, macOS, no errors. `COPY` into a label added the label and the folder kept the message. `UID EXPUNGE` in the label removed only the label. | Matches the effect table: `labels_apply` is `COPY`, `labels_remove` is expunge in the label mailbox. |
| 2026-10-10 | `MOVE` from a folder into a label behaved like `COPY`: the message stayed in the folder and gained the label. | `MOVE` into a label stays `unsupported_semantics`; reporting it as a move would be wrong. Use `labels_apply`. |
| 2026-10-10 | `MOVE` between two folders moved the message and kept its label. `APPENDUID` and `COPYUID` were reported. `APPEND` kept the requested INTERNALDATE. No `OBJECTID` capability. | Matches the effect table's note that moves keep labels. |
| 2026-10-10 | Every message carried `X-Pm-Internal-Id`, `X-Pm-Gluon-Id` and `X-Pm-External-Id` headers in both its folder and label occurrences. Whether the values match across occurrences was not recorded. | The probe now records, per header, whether the value is the same in every occurrence (never the values). |
| 2026-10-10 | All Mail showed none of the probe's messages during the run, including ones that existed; later reads found them. Expunge in a folder and deleting a non-empty folder left the message in no checked mailbox, including Trash. Because All Mail lagged, this does not show whether the message was destroyed. | Permanent deletion stays limited to Trash and Spam, and deleting a non-empty folder stays classed as possible destruction. The probe now polls All Mail (`--all-mail-wait`, default 30 s), with a control message that must appear, before reading absence as destruction. |
| 2026-10-10 | Bridge logged one HTTP 422 (code 2501, missing message) and one local-state cleanup error during the run. The cause was not established. The probe also re-examined and re-deleted the folder its folder-delete step had already removed. | The probe no longer inspects or deletes a mailbox it has already deleted. |
| 2026-10-10 | Second full run with All Mail polling. After expunge in a folder, the message appeared in All Mail after 24 s; after deleting a non-empty folder, its message appeared in All Mail after 10 s. The control message appeared after 10 s. | Neither operation destroyed the message in this configuration: it left the folder and stayed in All Mail. The effect table stays conservative (expunge only from Trash or Spam; deleting a non-empty folder is still treated as possible destruction) until more runs agree. |
| 2026-10-10 | `X-Pm-Internal-Id` (and the other `X-Pm-*` headers) had the same value in the folder, label and All Mail occurrences of one message. | A candidate for an account-scoped message identity. One message does not establish uniqueness or stability across resyncs or Bridge updates. |
| 2026-10-10 | Searches with `any_of` (OR) and `not` failed with "IMAP server rejected search". The adapter sent `OR ( FROM a ) ( SUBJECT b )`; RFC 3501 allows no space inside the parentheses, and Bridge rejects it. The fake test server accepted it. | Parentheses are now joined to their neighbours (`OR (FROM a) (SUBJECT b)`); a group ending in a non-ASCII literal gets a trailing `ALL`. A test checks the exact bytes sent. The probe has a `search` step that runs OR, NOT, nested and non-ASCII searches against Bridge. |
| 2026-10-11 | After the search fix, Bridge 03.27.01 matched the probe's OR, NOT and nested searches, and account-wide search with snippets worked. A subject search for a non-ASCII word missed the probe message, whose subject was raw UTF-8 header bytes rather than RFC 2047 encoded. | Real mail encodes non-ASCII subjects (RFC 2047), so the probe's required non-ASCII cases now use an encoded subject. The raw 8-bit form is kept as an informational case, and the probe records the subject Bridge reads back for both. The next run showed Bridge still missed encoded non-ASCII subjects (row below). |
| 2026-10-11 | With an RFC 2047 encoded subject, Bridge 03.27.01 still missed a non-ASCII subject search, and a NOT over a phrase with spaces failed to exclude a re-encoded subject, although both subjects read back decoded. Gluon's source (Bridge's IMAP library) confirms the cause: FROM, TO, CC, BCC, SUBJECT and HEADER searches are a lower-case substring match on the raw header, so text in an encoded header (`=?utf-8?q?...?=`) is not found, and BODY/TEXT match the raw message, so text inside base64 or quoted-printable parts is not found either. | Header-text searches are rechecked: the messages whose relevant headers are encoded are found with `HEADER <field> "=?"` (which raw matching handles), and for those alone the query is evaluated against the decoded headers; the server's answer stands for the rest. Body and text searches carry a note that encoded parts can be missed. |
| 2026-10-11 | Full probe run on Bridge 03.27.01 with the header recheck: exit 0, all search cases matched (OR, NOT, nested, non-ASCII, raw 8-bit), and drafts, folder delete and IDLE completed. SMTP was not part of the run. A Bridge log showed one local-state cleanup error (a missing local file); cause not established. | No change. |
| 2026-10-11 | A policy-governed send through MCP (`mail_send` with send = ask): `approval_pending`, owner approval, `operations_resume`, SMTP 250. The immediate result had no Sent copy; `mail_reconcile` then found exactly one by Message-ID, with the requested sender, recipient, subject and body. The Sent copy also carried the account's public key as an attachment (Proton's attach-public-key setting). Delivery to the recipient's inbox was not checked; the reply path was not exercised. | The deployment guide says to reconcile after a send and that Proton may add the public key to the Sent copy. |

## Rerunning tests

Rerun the live acceptance tests:

- When Proton Mail Bridge is updated, including minor updates.
- Periodically, for behaviour that may depend on remote feature flags. The cadence has not been set yet.
- When a configuration change could affect results, such as the address mode or a Bridge setting.

When a rerun fails, change the row to `unverified` for that version, or record the failure as a known limit. Do not delete the old evidence without a note.

## Unknowns

- Only Bridge 03.27.01 on macOS has been tested live, on one account (see Observed Bridge behaviour). The packaged evidence file is empty, so every Bridge version field is `—` until you record your own probe results.
- Not yet exercised live: replies (`mail_reply`), forwards, recipient-side delivery, Linux and Windows.
- The fake servers do not model every Bridge behaviour. Unknown behaviour is not covered by the fake-server tests.
