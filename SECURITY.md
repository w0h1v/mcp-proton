# Security policy

mcp-proton is an independent project and is not affiliated with or endorsed by Proton AG.

## Supported versions

The project is pre-1.0. Only the latest release receives security fixes. No release has been published yet.

| Version | Supported |
|---|---|
| Latest release | Yes |
| Older releases | No |

## Reporting a vulnerability

Use GitHub private vulnerability reporting on this repository. Open the repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue or pull request for a suspected vulnerability.

Include:

- The mcp-proton version or commit, and the deployment model (personal local or isolated service).
- Steps to reproduce, and the expected and actual results.
- The impact you believe the issue has.

Do not include real mailbox contents, credentials or tokens in a report. Redact them, or use a test account.

The repository publication step sets up private vulnerability reporting. Until then, this policy describes the intended channel and no reports can be received through it.

## Scope

In scope:

- **Policy bypass.** Any path that performs a Deny or Ask operation without the required decision, through MCP tools, resources, jobs, CLI or HTTP.
- **Approval replay or tampering.** Reuse of an approval for a different payload, account, recipient set or expiry. Resuming an operation with replacement arguments.
- **Credential leakage.** Secrets, bearer tokens or the owner credential written to logs, diagnostics, operation records, error messages or transcripts that mcp-proton produces.
- **Path traversal.** Reads or writes outside the configured attachment import and export directories, or through crafted attachment filenames.
- **HTML sanitization.** Script or active content, or remote-asset fetches, that get past the message rendering sanitizer.
- **Header injection.** Injection of extra headers or recipients through composed or forwarded message fields.
- **Authentication and origin checks** on the HTTP transport and the owner interface, including CSRF.

Out of scope:

- **Proton Mail, Proton Mail Bridge and other Proton services.** Report these to Proton through Proton's own security channels. This project cannot fix them.
- **Agents acting within granted permissions.** A permitted action that an agent takes on untrusted email content is a policy design question, not a vulnerability. See the trust boundaries below.
- **Local attackers with the same OS user.** The personal local deployment does not isolate the agent from the owner's account. Reports that depend only on that shared trust are out of scope, but reports about the isolated service deployment are in scope.
- **Findings from automated scanners** without a working reproduction.

## Trust boundaries

Two deployment models have different guarantees. See `docs/deployment.md`.

**Personal local deployment.** The agent and mcp-proton run as the same OS user. Application policy governs calls made through mcp-proton. It does not stop an agent that has unrestricted shell or file access in that same user account. Such an agent can edit the configuration, read the Bridge credentials, or talk to Bridge directly. Use this model for trusted agents only.

**Isolated service deployment.** The service, its credentials, the policy files and the owner approval interface run under a separate OS user. The agent reaches the service over authenticated HTTP with scoped, revocable client tokens. This model depends on the operator enforcing the boundary: separate users alone are not enough if the agent can still read Bridge credentials, reach an unprotected admin endpoint or elevate privileges. The operator must check the boundary in their own environment.

Neither model protects content from the agent itself. Email read by an agent can contain instructions. The Autonomous preset allows sending and exporting without review.

## Data at rest

mcp-proton stores operational records in a local SQLite database, including pending approval payloads. Encryption at rest is not implemented. See `docs/storage.md`.
