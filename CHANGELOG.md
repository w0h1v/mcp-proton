# Changelog

All notable changes are recorded here. The project follows semantic versioning from 1.0; before that, tool schemas, preset membership and storage formats may change between alpha releases, and each change will be listed here.

## Unreleased — 0.1.0a1

Not released. Implemented and tested against fake IMAP/SMTP servers only; not yet verified against a live Proton Mail Bridge.

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
