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
