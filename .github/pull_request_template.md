## Summary

<!-- What changes, and why. Link the design section or issue if one applies. -->

## Policy classification

- Operation family:
- Effect semantics (entry in `src/mcp_proton/services/effects.py`):
- Default preset behaviour (Reader / Assistant / Autonomous):

<!-- Write "not applicable" if this change does not add or change an operation. -->

## Tests

- [ ] `ruff check src tests`
- [ ] `mypy`
- [ ] `pytest -q -m "not live"`
- Added or changed tests:

## Compatibility evidence

<!--
Fake-server tests are not Bridge compatibility evidence.
State one of:
- No Bridge behaviour changed. Not applicable.
- Live evidence attached: Bridge version, test date, configuration, result.
- No live evidence yet. The operation stays "unverified" in docs/compatibility.md.
-->

## Checklist

- [ ] No mailbox contents, addresses, credentials, tokens or personal data in code, fixtures, logs or this description.
- [ ] Fixtures are synthetic.
- [ ] No code copied from Mailpouch or any other project. Provenance and license notices are included if third-party material was used.
- [ ] Docs are updated (`docs/compatibility.md`, `docs/storage.md`, or `docs/deployment.md`) where behaviour, storage or permissions changed.
- [ ] Changes to tool schemas, preset membership, persistence or effective permissions are announced in the release notes.
- [ ] The change does not claim Proton endorsement or affiliation.
