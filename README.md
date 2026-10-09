# mcp-proton

An MCP server that gives AI agents access to Proton Mail through [Proton Mail Bridge](https://proton.me/mail/bridge). You, the owner, decide what each agent may do: read, organize, draft, send or delete. Every operation goes through one policy engine, whether it comes from an MCP client, the CLI, the review UI or a scheduled job.

Independent project. Not affiliated with or endorsed by Proton AG.

**Status: alpha, unreleased.** Everything in the design is implemented and tested against fake IMAP/SMTP servers. **Nothing has been verified against a real Proton Mail Bridge yet.** Every capability reports as `unverified` until you run the compatibility probe against a dedicated test account (see [Compatibility](#compatibility)). A license has not been chosen yet.

## Requirements

- A paid Proton Mail plan that includes Bridge, with Bridge installed and signed in.
- Python 3.11 or newer.
- macOS is the first development target. Linux and Windows are untested: credential store, TLS, filesystem and service behaviour have not been checked there.

## Install

```sh
uv venv && uv pip install -e .      # or: pip install -e .
mcp-proton --help
```

## Set up

```sh
mcp-proton init
```

Setup walks through:

1. **Connect Bridge.** Host and ports (default IMAP 1143, SMTP 1025), your Bridge username and Bridge password. The password goes to the OS credential store; for headless systems use `--secret-ref env:VAR` or `--secret-ref file:/path` (mode 0600). There is no plaintext fallback.
2. **Trust Bridge's certificate.** Bridge uses a self-signed certificate. Setup shows its SHA-256 fingerprint and pins it after you confirm. You can use `--tls-ca-file` with Bridge's exported certificate instead.
3. **Choose an autonomy preset** (below). Until you choose one, every operation is denied.
4. **Choose storage mode.** `live` (default) keeps no copy of your mail. `metadata` and `index` keep a local cache for faster listing and full-text search.
5. **Print a client configuration** for your MCP client.

## Autonomy presets

| Preset | Reading | Organizing and drafts | Sending | Permanent deletion | Import, export, folder/label deletion, automation |
|---|---|---|---|---|---|
| Reader | Allow | Deny | Deny | Deny | Deny |
| Assistant | Allow | Allow | Ask | Ask | Ask |
| Autonomous | Allow | Allow | Allow | Allow | Allow |
| Custom | your choice | your choice | your choice | your choice | your choice |

- **Allow** runs immediately. **Deny** refuses. **Ask** records the request and returns `approval_pending`; it runs only after you approve it with `mcp-proton approvals approve <id>` or in the review UI, and the agent calls `operations_resume`. An approval covers the exact request (recipients, content, targets). Any change needs a new approval.
- Override any family per client, account or mailbox: `mcp-proton policy set send deny --client hermes`. Grant temporary access: `mcp-proton policy grant send --minutes 30`.
- Limits that apply on top of any preset: allowed accounts and mailboxes, allowed recipients or domains, batch size, sends per day, and the directories agents may read attachments from or write exports to. `mcp-proton policy constraints set --help`.
- See the effective setting and where it comes from: `mcp-proton policy show --client hermes --account personal`.

**Autonomous mode:** an agent that reads untrusted email can be manipulated by that email into sending or exporting information. Consider a recipient restriction (`--allowed-recipients @yourdomain.com`) alongside it.

## Connect an MCP client

Local stdio (simplest; Hermes and most desktop clients):

```sh
mcp-proton client-config hermes --client-id hermes      # or: claude-desktop, generic
```

The client launches `mcp-proton serve --transport stdio` with `MCP_PROTON_CLIENT_ID` set. That ID is a label for organizing policy. It is not authentication: any process running as your OS user can claim it.

Authenticated HTTP (multiple clients, or agents on another machine through a tunnel):

```sh
mcp-proton clients add hermes --http-token     # prints a bearer token once
mcp-proton serve --transport http              # binds 127.0.0.1:8765 by default
```

Each token maps to one client identity. Revoking a client (`mcp-proton clients revoke hermes`) takes effect on its next request.

## Trust boundaries

- **Personal local deployment.** The agent and mcp-proton run as the same OS user. Policy governs calls made through mcp-proton, but an agent with unrestricted shell or file access can edit the policy file or talk to Bridge directly. Fine for trusted personal agents.
- **Isolated service deployment.** mcp-proton, its credentials, policy and the review UI run as a separate OS user the agent cannot access, and agents connect over authenticated HTTP. See [docs/deployment.md](docs/deployment.md).

Neither the transport nor an approval prompt proves isolation by itself.

## Where your email goes

mcp-proton makes no AI calls and sends mail content nowhere except the MCP client that asked for it. What the agent does with that content is outside mcp-proton's control: if your agent uses a hosted model, email content reaches that provider and may persist in transcripts or logs. Bridge keeps its own local cache. To limit what this service releases, set `release_bodies = false` or `release_attachments = false` in the policy constraints.

mcp-proton stores operational records even in `live` mode: the operation journal (including the full content of requests awaiting approval), change events (no subjects, addresses or bodies), mailbox state snapshots, jobs and managed attachment copies. See [docs/storage.md](docs/storage.md). `mcp-proton purge` applies retention.

## What agents can do

Tool families: `accounts_*`, `mailboxes_*`, `messages_*`, `labels_*`, `attachments_*`, `drafts_*`, `mail_send`/`mail_reply`/`mail_forward`/`mail_reconcile`, `imports_*`, `exports_*`, `events_*`, `conversations_get`, `operations_*`, `jobs_*`, `rules_*`, `webhooks_*`, `saved_searches_*`, and with a local index `search_local` and `stats_get`. Resources expose capability reports, mailbox lists, operation status and message content under opaque `proton://` URIs, with the same policy checks as tools.

Behaviour worth knowing:

- Reading never marks a message read. Use `messages_set_flags` to do that.
- Message handles are opaque. They include the mailbox's UIDVALIDITY, so a handle that has gone stale fails instead of acting on the wrong message.
- Proton labels appear as mailboxes under `Labels/`. Adding a label copies into that mailbox. Removing a label removes only that occurrence. Operations whose effect on Proton's model is not established return `unsupported_semantics` instead of guessing.
- Permanent deletion works only from Trash or Spam, removes exactly the targeted messages, and never runs a bare `EXPUNGE`. Emptying Trash uses a snapshot, so mail that arrives during the operation is kept.
- A successful send means Bridge accepted the message, not that recipients received it. If the connection drops after the message was handed over, the result is `delivery_unknown`, and it is **never resent automatically**. `mail_reconcile` looks for it in Sent. Not finding it there does not prove it wasn't delivered.
- Scheduled sends, reminders, snooze and filing rules are **local automation**. They need mcp-proton running (`serve`) on this machine. They are not Proton's native scheduled send or server-side filters.

Outside Bridge, not available: native scheduled send management, contacts, calendar, server-side filters, address provisioning and key management.

## Owner tools

```sh
mcp-proton approvals list | show <id> | approve <id> [--execute] | deny <id>
mcp-proton activity                 # what happened, including partial results
mcp-proton pause <account>          # stop all access to an account now
mcp-proton clients revoke <id>
mcp-proton ui-token && mcp-proton ui   # local review UI on 127.0.0.1:8766
mcp-proton diagnostics --json       # metadata only; safe for bug reports
```

## Compatibility

Each capability reports `available`, `unavailable` or `unverified` for the connected Bridge (`accounts_capabilities`). It becomes `available` only with recorded evidence for that Bridge version.

To produce evidence, run the probe against a **dedicated test account**, never your personal mailbox:

```sh
mcp-proton probe test-account --yes-dedicated-test-account --send-to you@example.com --out report.json
# review report.json, then:
mcp-proton probe test-account --yes-dedicated-test-account --record
```

The probe creates its own folders, label and synthetic messages, records how Bridge actually behaves (label semantics, expunge outside Trash, Sent filing, imported dates and more), and cleans up. Rerun it after Bridge updates. See [docs/compatibility.md](docs/compatibility.md).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). Tests run against fake IMAP (pymap) and SMTP (aiosmtpd) servers; they do not need a Proton account and are not Bridge evidence. Live tests are opt-in: `MCP_PROTON_LIVE=1 MCP_PROTON_LIVE_ACCOUNT=<name> pytest -m live`.

Design: [docs/design.md](docs/design.md). Roadmap: [docs/roadmap.md](docs/roadmap.md). Security reports: [SECURITY.md](SECURITY.md).
