# Roadmap

This roadmap follows the phased delivery in `docs/design.md`. Phases control release order. They are not a list of completed features.

## Current status

- The code for the Phase 0 to Phase 5 scope is in this repository.
- **None of it is verified against a live Proton Mail Bridge.** All tests use fake IMAP and SMTP servers.
- **Nothing is released.** There is no published package and no tagged version.
- **License: Apache-2.0.** Runtime dependencies are Apache-2.0, MIT or BSD-3-Clause (checked 2026-10-09).
- The compatibility matrix in `docs/compatibility.md` has no live Bridge evidence. Every operation is `unverified`.

## Phases

| Phase | Deliverables | Exit evidence | Status |
|---|---|---|---|
| 0. Foundation and compatibility | Project skeleton, pinned dependencies, typed domain contracts, policy engine, operation journal, owner CLI skeleton, isolated-deployment design, disposable mailbox spike | Actual capabilities and flags, and address modes, recorded for a tested Bridge version. Effect table for moves, expunge and folder/label deletion. Identity strategy. Sent, draft, SMTP and import probes. Hermes protocol and approval checks. Polling and connection budget. | Code present, including the `probe` command. No probe results recorded. |
| 1. Public alpha | Local stdio and one documented authenticated HTTP deployment. Reading, search, attachment retrieval and scoped ingestion, flags, filing, labels, folder creation, drafts, sending and replies. All presets. CLI approvals, status and resume. Live-access storage. | Every exposed tool passes domain policy and operation-journal tests. Key Bridge round trips. No duplicate send on replay. Unknown-delivery handling. Reproducible install and synthetic CI. Explicit supported and unsupported matrix. | Code present. Round trips against Bridge not run. |
| 2. Complete Bridge beta | Remaining folder and label management and deletion, targeted expunge, bulk operations, EML and mbox import and export, subscriptions where supported, complete attachment and MIME handling, multi-account and address edge cases, change tracking, conversation presentation. | Every inventory row implemented and verified, explicitly unsupported with evidence, or recorded as a release blocker. Membership and identity tests. Interrupted and concurrent-operation recovery. Large-mailbox measurements. | Code present for many rows. Two inventory rows are not implemented (see `docs/compatibility.md`). No acceptance evidence recorded. |
| 3. Stable 1.0 | Hardened Bridge layer, local administration and review UI, packaging, upgrade and migration path, documentation and release artifacts. | No unexplained inventory gaps. Declared OS and client support backed by tests. Independent security and reliability review. Clean install, upgrade and uninstall. Tested deployment boundaries. Release rollback guidance. | Local owner review UI implemented (`mcp-proton ui-token`, `mcp-proton ui`); packaging, migration path and independent review not done. |
| 4. Optional automation | Durable schedules, reminders, local rules, event consumers and webhooks, best-effort undo. | Restart and missed-run recovery. Policy rechecks. Scoped event delivery. Explicit limits for irreversible operations. Feature-specific acceptance cases. | Job store present (create, claim, run records). A scheduler loop was not found in the code reviewed. No acceptance evidence recorded. |
| 5. Optional search and storage expansion | Metadata and full-text caching, saved searches, statistics, attachment extraction, optional semantic search, encryption-at-rest modes. | Retention and purge verification. Indexing correctness. Privacy documentation. Data migrations. Storage and performance measurements. Provider opt-in. | Code present. Encryption at rest is not implemented. |

## Before a first public release

These items are separate execution steps from the design work:

1. ~~Choose and add the license, and check dependency-license compatibility.~~ Done: Apache-2.0.
2. Verify the phase exit evidence against a dedicated Bridge test account with `mcp-proton probe`, review the results, and record them in the packaged `src/mcp_proton/compatibility.json` and `docs/compatibility.md`. Fix the evidence-file location mismatch first (see `docs/compatibility.md`).
3. Set the enforcement contact in `CODE_OF_CONDUCT.md` and confirm private vulnerability reporting in `SECURITY.md`.
4. Confirm the repository owner and the package name.
