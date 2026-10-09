# Contributing to mcp-proton

mcp-proton is an independent project. It is not affiliated with or endorsed by Proton AG.

## Development setup

Requires Python 3.11 or later and [uv](https://docs.astral.sh/uv/).

```sh
uv venv
uv pip install -e '.[dev]'
```

Run the checks before opening a pull request:

```sh
ruff check src tests
mypy
pytest -q -m "not live"
```

`mypy` reads its configuration from `pyproject.toml`.

## Tests

- Unit and service tests use a fake IMAP server (pymap) and a fake SMTP server (aiosmtpd).
- Fake-server tests check our code paths, fault handling and policy behaviour. They are **not** evidence that Proton Mail Bridge behaves the same way. Do not cite them as compatibility evidence.
- Live Bridge tests are opt-in. They live in `tests/live/`, carry the pytest marker `live`, and run only when `MCP_PROTON_LIVE=1` is set. Name the test account with `MCP_PROTON_LIVE_ACCOUNT`.
  - Use a dedicated test account only. Never use a personal mailbox.
  - Never run live tests on untrusted pull requests or with secrets from untrusted code. The `live-bridge.yml` workflow is manual (`workflow_dispatch`) and runs on a self-hosted runner.
  - The probe (`mcp-proton probe`) creates and deletes its own mailboxes and synthetic messages in the test account. Read `docs/compatibility.md` before running it.
- Synthetic MIME fixtures live in `tests/fixtures/mime/`. Do not add real messages, real addresses or real credentials to fixtures.

## Adding a capability

A pull request that adds a Bridge operation must include all of the following:

1. **Policy family classification.** Assign the operation to an existing `OperationFamily` in `src/mcp_proton/domain/families.py`, or explain why a new family is needed. Classify by expected effect, not by IMAP verb.
2. **Effect semantics.** Add an entry to the effect table in `src/mcp_proton/services/effects.py` for each mailbox type the operation touches. Combinations without an entry return `unsupported_semantics`. New entries start with `verified=False`.
3. **A fixture and a test.** Use a fake-server or synthetic fixture for the protocol behaviour. Cover the policy decision and the journal behaviour where the operation can change state.
4. **Compatibility evidence.** State what was tested against a real Bridge, or state that no live evidence exists yet. Record the Bridge version, test date and configuration as described in `docs/compatibility.md`. Until that evidence exists, the operation stays `unverified`.

Do not describe a capability as supported on the basis of fake-server tests.

## Code of conduct

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Third-party code and provenance

Mailpouch is a feature reference only. Do not copy code, tests, fixtures or documentation from Mailpouch or from any other project unless you can show its provenance and license, and you add the required notice in the same pull request. By contributing, you agree that your contribution is licensed under Apache-2.0 (section 5 of the license). Write new code from the design in `docs/design.md` and from Proton's public documentation.

If you add a dependency, check that its license is compatible with the project license, Apache-2.0. Copyleft licenses (GPL, AGPL, LGPL) need a maintainer decision first. The dependency review workflow reports new dependencies on pull requests.

## Security issues

Do not open a public issue for a vulnerability. Follow [SECURITY.md](SECURITY.md).

## Pull requests

- Keep each pull request to one logical change.
- Fill in the pull request template.
- Do not include mailbox contents, credentials, tokens or personal data in issues, logs, fixtures or commit messages.
