# Storage and data handling

This page describes what mcp-proton stores on disk, where it is stored, and how long it is kept. It applies to mcp-proton only. Bridge keeps its own cache, and agents can keep email in transcripts, memory or provider logs. mcp-proton cannot control those.

## Storage modes

The `storage_mode` setting in `config.toml` selects one of three modes:

| Mode | What mcp-proton keeps about messages | Operational records |
|---|---|---|
| `live` (default) | No message cache. Message content is read from Bridge on each request. | Kept. See below. |
| `metadata` | Caches headers and flags for faster listing. | Kept. |
| `index` | A full local searchable index, which can include message bodies. | Kept. |

The first release supports `live` access. The `metadata` and `index` modes are selectable in the configuration, but this repository does not yet verify them against a live Bridge. Check the release notes before relying on either mode.

**Operational records exist in every mode.** Live mode is not zero persistence. The records below are kept even when no message cache is kept.

## Operational records

All records are in one SQLite database.

| Table | What it holds | Why it is kept |
|---|---|---|
| `operations` | Each write request: its kind, family, client, account, status, digest, the **exact executor payload** (`request_json`), a summary, the result and error, the outgoing Message-ID for sends, and timestamps. | Pending approvals need the exact payload so that an approved operation can run later without replacement arguments. Also used for duplicate protection and the activity log. |
| `operation_items` | One row per target in an operation: target, status, detail, new handle, and `prior_state` as JSON. | `prior_state` is the recorded state used for best-effort undo of reversible operations. |
| `events` | Change events for an account: type, mailbox, and a JSON data field. | Event journal and polling cursor for agents. The design limits this to metadata. |
| `mailbox_state` | Per-mailbox UIDVALIDITY, UIDNEXT, a flag snapshot keyed by UID (`flags_json`), and last reconciliation time. | Change tracking and reconciliation. |
| `jobs` and `job_runs` | Scheduled items (scheduled send, reminder, snooze, rule, webhook): their specification as JSON, next run, status and run results. | Durable schedules (Phase 4). A scheduled send's specification can include outgoing content, as the design requires. |
| `artifacts` | Metadata for managed attachment bytes: account, source handle, filename, content type, size, path and expiry. | Attachment handles for outgoing mail and exports. The bytes are stored as files, not in the table. |

In `live` mode, incoming message bodies, headers and attachment bytes read from Bridge are not kept in these tables. Two places can hold outgoing content:

- `operations.request_json` holds the executor payload. For a send, a draft or a pending approval, this includes the outgoing message as submitted: subject, recipients, plain-text and HTML body, and attachment references.
- Attachment bytes held as managed artifacts are files under the artifact directory. Local attachment files are read from the path given at request time, and their digest is recorded so that a changed file is detected.

## Where the files are

Default locations come from `platformdirs` for the application name `mcp-proton`:

| Item | Default location | Override |
|---|---|---|
| Configuration (`config.toml`, `policy.toml`) | User config directory for `mcp-proton` | `MCP_PROTON_CONFIG_DIR` |
| Database `mcp-proton.sqlite3` | `<data_dir>/mcp-proton.sqlite3`, where `<data_dir>` is the user data directory for `mcp-proton` | `data_dir` in `config.toml` |
| Managed artifacts | `<data_dir>/artifacts` | `artifact_dir` in `config.toml` |

The database uses SQLite in WAL mode. mcp-proton sets the database file to mode `0600`. The WAL and shared-memory side files are created next to it by SQLite and are not separately chmod-ed in the code reviewed here. Check their modes in your environment.

The attachment service documents artifact directories as mode `0700` and files as `0600`. Confirm the modes on your system after the first attachment is stored.

Secrets are not in these files. `config.toml` references secrets (`keyring:`, `env:` or `file:`). A `file:` secret must be mode `0600`.

## Retention

- `retention_days` in `config.toml` defaults to 30 and must be at least 1. The attachment service uses it to set the expiry of new managed artifacts.
- `mcp-proton purge --older-than-days N` deletes terminal rows from `operations` older than N days. It also marks pending or approved operations past their expiry as `expired`. The command takes its own age argument and does not read `retention_days`.
- The `purge` command, as implemented, deletes only from `operations`. It does not delete from `events`, `mailbox_state`, `jobs`, `job_runs` or `artifacts`. Expired artifact files are not removed by `purge` in the code reviewed here.
- Trash keeps a message on the server only until Bridge or Proton removes it. Provider retention settings are outside mcp-proton.

Expired artifacts are refused when read, but the code reviewed here has no scheduled removal of expired artifact files or rows. Records in the other tables are not expired by any code reviewed here. Treat these as open items until a later release documents them.

## Encryption at rest

**Not implemented.** The database, managed artifacts and any cached content are plain files protected by OS permissions only. Backups of the data directory have the same exposure. Encryption at rest is planned for Phase 5 and is not started.

If you need encryption now, use full-disk encryption on the host, and restrict the data directory to the service user.

## Deleting data

- `mcp-proton purge --older-than-days N` removes old operation records as described above.
- Deleting the data directory removes all operational records and artifacts. Do this only when no approval is pending, since pending approvals are stored there.
- `mcp-proton accounts remove NAME` removes the account from `config.toml` only. Keyring secrets are left in place, and the operational records are not deleted.

## What is not stored

mcp-proton does not store the Bridge password in its files. It does not run an AI model, and it does not send data to a model provider. Diagnostics (`mcp-proton diagnostics`) are metadata only: versions, configuration summary, preset and counts. They contain no addresses, credentials or message contents, and mailbox names only when `--include-mailboxes` is given.

## Agent transcripts and logs

A local connection does not control what the agent does with returned email. Anything an agent reads can appear in its transcript, its memory, or the logs of a model provider, depending on how the agent is set up. mcp-proton does not control those stores.
