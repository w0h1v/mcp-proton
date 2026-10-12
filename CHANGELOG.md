# Changelog

All notable changes are recorded here. The project follows semantic versioning from 1.0; before that, tool schemas, preset membership and storage formats may change between pre-1.0 releases, and each change will be listed here.

## Unreleased

### Added
- Setup takes Bridge's connection mode (`--security starttls|ssl`, for IMAP and SMTP), address mode (`--address-mode combined|split`) and extra sender addresses (`--identity`, combined mode only). Before, SSL mode and identities needed hand edits to `config.toml`, and the address mode was never recorded. The certificate fingerprint is read over implicit TLS when the mode is SSL. `accounts list` shows the address mode and identities.
- Docs: a Proton Mail Bridge page covering setup from Mailbox details, the Bridge settings that matter (ports, connection mode, exported certificates, Show All Mail, split addresses, repair and reset), Bridge behaviour agents will notice, and troubleshooting by error code.

## 0.2.0b2 — 2026-10-12

Security-fix beta: upgrade from 0.2.0b1. Two behaviour changes: under the Assistant preset `drafts_discard` now asks for approval, and a draft discard already waiting for approval before upgrading fails on resume and must be requested again.

### Security
Fixes from an automated security review of 0.2.0b1.
- `mail_reply` with a quote, and `mail_forward`, copy the original's content into the new message, so they now honour `release_bodies` (and `release_attachments` when forwarding as an attachment). Before, they bypassed both. A reply with `quote=false` still works when bodies are restricted.
- `drafts_discard` deletes a draft permanently, so it now also needs the permanent-deletion permission. Under the Assistant preset it asks for approval instead of running at once.
- `events_reconcile` without a mailbox, and `events_watch_status`, now cover only the watched mailboxes the caller may read; before, a client limited by `allowed_mailboxes` could reconcile, and see the names and activity of, every watched mailbox.
- `accounts_list` no longer shows a paused account to a client whose allowed accounts exclude it.
- Outgoing messages and draft updates have size limits, checked before anything is built or journaled: subject 2,000 characters, text and HTML 2,000,000 each, 100 addresses in each of to/cc/bcc/reply-to, 50 attachments, 200 references. Sending a stored draft checks the recipients and subject it journals against the same limits; the draft's body and attachments are sent as stored.
- Webhooks: at most 10 per client and 100 in total; one delivery attempt per destination per scheduler pass (failures back off as before, without blocking), and a 15-second delivery budget per pass (checked before every event), so unreachable destinations cannot delay scheduled sends and other jobs.
- Imports: at most 1,000 files and 100,000 messages per request, and the owner's `max_batch_size` is applied while counting, before files are hashed. An mbox message over 100 MB is rejected while it is read instead of after it is held in memory.
- Import, export and attachment files are opened inside their configured directories one path component at a time without following symbolic links, and read, hashed and written through those descriptors. A client that can write in a shared directory can no longer swap in a link after the path check to read a service-only file or write outside the directory. Export of messages now also honours the client's own export directories, not only the global ones.
- Reading mail walks at most 1,000 MIME parts nested 32 deep, stops decoding body parts once the body limit is reached, and measures base64 attachments without decoding them.
- Docs: the deployment guide describes the review UI as shipped, says the HTTP transport has no built-in TLS, and states what webhook creation lets a client reach; the README notes webhooks among the places the service sends data.

## 0.2.0b1 — 2026-10-12

First beta. The feature set in the design is complete and has been exercised on a live Proton Mail Bridge; tool schemas and storage formats may still change before 1.0. No code changes since 0.1.0a1.

### Changed
- Version and package classifier move from alpha to beta (`Development Status :: 4 - Beta`).
- Docs: the roadmap, compatibility page and README describe the release and what was tested live; the README tool table lists every tool group's tools.

### Still to do before 1.0
- Live tests of replies, forwards and recipient-side delivery.
- A second Bridge version or operating system.
- Reviewed probe evidence in the packaged `compatibility.json`.
- Independent security review; clean install, upgrade and uninstall checks.

## 0.1.0a1 — 2026-10-11

First alpha. Tested against fake IMAP/SMTP servers and on one live Proton Mail Bridge 03.27.01 account (macOS): the full compatibility probe (mailboxes, flags, labels, moves, expunge, trash, identity, import dates, search, drafts, folder delete, IDLE) and one policy-governed send with owner approval. Not yet exercised live: the reply path, recipient-side delivery, other Bridge versions. Known limit: Bridge matches body and text searches against the raw message, so words inside encoded parts can be missed. See docs/compatibility.md.

### Added
- Licensed under Apache-2.0.
- Policy engine with Reader, Assistant, Autonomous and Custom presets; per-client, per-account and per-mailbox rules; temporary grants; scope, recipient, path, batch and daily-send constraints.
- Durable operation journal with an approval workflow (`approval_pending`, owner approval, `operations_resume`), atomic execution claims and idempotency keys.
- An effect table that classifies each mail operation by its effect on Proton's model (label removal vs permanent deletion, and so on).
- IMAP adapter (connection pool, dedicated IDLE connection, UIDVALIDITY checks, PEEK reads, UIDPLUS-only targeted expunge), MIME parsing and building with HTML sanitization, SMTP transport with per-recipient results and `delivery_unknown`.
- Services: accounts, mailboxes, folders, labels, messages, search, flags, filing, deletion, bulk preview, drafts, sending, replies, forwards, attachments, EML/mbox import and export, change tracking, conversation grouping.
- FastMCP 4.1 server over stdio and authenticated HTTP: tools, resources and prompts, plus elicitation review for clients the owner trusts.
- Owner CLI: setup, policy, clients, approvals, activity, diagnostics, retention, compatibility probe.
- Local owner review UI.
- Optional local automation: scheduled sends, reminders, snooze, rules, webhooks, best-effort undo.
- Optional metadata cache and full-text index, with saved searches and stats.

### Changed after live Bridge testing
- The IMAP adapter no longer names possibly-missing UIDs in `UID SEARCH`; Bridge answers `NO no such message` instead of an empty result.
- The probe paces its steps (`--pace`), can stop at the first error (`--stop-on-error`), and can list and remove leftover probe mailboxes (`--cleanup`).
- Probe labels end in `-label`: Proton rejects a label with the same name as a folder. A setup failure is recorded in the report and the probe exits 1.
- The probe polls All Mail, which Bridge fills late, before reading a missing message as destroyed (`--all-mail-wait`), records whether Proton's `X-Pm-*` identity headers match across a message's occurrences, and no longer inspects or deletes a folder it already deleted.
- Searches using `any_of` (OR) or `not` are sent in the form RFC 3501 requires; Bridge rejected the previous form.
- The probe checks OR, NOT, nested and non-ASCII searches against Bridge, and `probe --record-report` records a reviewed report without rerunning the probe.
- `messages_search` takes `account_wide` to search every readable mailbox, including All Mail. Over several mailboxes, each result is one message with an `occurrences` list, grouped by Proton's message ID (`X-Pm-Internal-Id`), else by Message-ID.
- Search filters `has_attachment`, `attachment_name` (filename contains, case-insensitive) and `attachment_type` (`application/pdf`, or a prefix such as `image/`). IMAP cannot search them, so they are applied from each match's MIME structure; they apply to bulk previews and rules too.
- `messages_get_many` reads up to 20 messages in one call, each with its own result or error.
- `messages_search` takes `snippets` to add a short excerpt of each result's body, centred on the first body/text term. It reads only the first 4 KB of each message's text part, and is omitted when policy does not release bodies.
- `messages_search` takes `wait_for_match` (0-60 s): while nothing matches, the first page is searched again every 2 s. The result reports `waited_seconds`, and a miss after the full wait is labelled as not proof of absence, since Bridge applies changes late.
- The probe's non-ASCII search cases use an RFC 2047 encoded subject, as real mail does; a raw 8-bit subject is checked for information only, and the subjects Bridge reads back are recorded.
- Header-text searches (from, to, cc, bcc, subject, header) find messages whose headers are RFC 2047 encoded. Proton Bridge matches the raw header, so these were missed, including ASCII words in subjects that senders had encoded; such messages are now rechecked against their decoded headers. Body and text searches note that Bridge can miss words inside base64 or quoted-printable parts.
- `policy show --client <id>` shows that client's own constraints, review channel and revoked state alongside the global constraints.
- The release workflow's archive check no longer fails on a broken pipe (`grep -q` closing the listing early under `pipefail`).
