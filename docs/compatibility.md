# Bridge compatibility matrix

This matrix records which Proton Mail Bridge operations mcp-proton has been tested against, and on which Bridge version. It is a template with no live evidence yet.

Every row is `implemented, unverified` or `not implemented`. No row has live Bridge evidence. Fake-server tests do not count as compatibility evidence.

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

3. Merge the reviewed observations into the evidence file with `--record`:

   ```sh
   mcp-proton probe <test account> --yes-dedicated-test-account --record --reviewed-by "<reviewer>"
   ```

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
| 2026-10-10 | Bridge 03.27.01 rejected creating a label whose name matched an existing folder: HTTP 409, code 2500, "Label or folder with this name already exists". The IMAP `CREATE` failed with a conflict. Folders and labels share one name namespace. | Probe labels end in `-label`, so every probe mailbox has a distinct name. A setup failure is now recorded in the report (`stopped_at: setup`, `created_mailboxes`) and the probe exits 1 instead of aborting without a report. |
| 2026-10-09 | An unpaced full probe run was followed by a Bridge sync error (UserBadEvent). A paced rerun (stop on first error) did not reproduce it. Cause not established. | The probe pauses between steps by default (`--pace`, 2 s) and supports `--stop-on-error`. |

## Rerunning tests

Rerun the live acceptance tests:

- When Proton Mail Bridge is updated, including minor updates.
- Periodically, for behaviour that may depend on remote feature flags. The cadence has not been set yet.
- When a configuration change could affect results, such as the address mode or a Bridge setting.

When a rerun fails, change the row to `unverified` for that version, or record the failure as a known limit. Do not delete the old evidence without a note.

## Unknowns

- No Bridge version has been tested. Every Bridge version field is `—`.
- The fake servers do not model every Bridge behaviour. Unknown behaviour is not covered by the fake-server tests.
