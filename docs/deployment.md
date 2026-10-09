# Deployment

mcp-proton supports two deployment models. Pick the one that matches the boundary you want. Neither model is more "secure" in general. Each gives different guarantees.

mcp-proton is an independent project. It is not affiliated with or endorsed by Proton AG.

## Choosing a model

| | Personal local | Isolated service |
|---|---|---|
| Who runs the agent and mcp-proton | The same OS user | Different OS users (or machines) |
| Transport | stdio (the client starts mcp-proton) | Authenticated HTTP on loopback, or a stdio connector |
| Credentials | In the owner's keyring, reachable by the same user | Held by the service user only |
| What policy protects against | Well-behaved agents calling mcp-proton | An agent that can reach the service but not the credentials, the policy files or the owner interface |
| What it does not protect against | An agent with shell or file access in the same user account. It can edit config or connect to Bridge directly | Misconfiguration of the OS boundary, and a compromised service user |

If the agent has unrestricted shell access in the same OS account, application policy does not stop it. In that case the personal local model is a convenience, not a security boundary.

## Personal local

Use this model for a trusted agent that you run as your own user.

1. Install mcp-proton in a virtual environment.
2. Run setup and add an account. Store the Bridge password in the OS keyring.
3. Choose a preset with `mcp-proton policy preset <reader|assistant|autonomous>`, or define a custom policy.
4. Print the client configuration and add it to the client:

   ```sh
   mcp-proton client-config hermes --client-id hermes
   ```

   The client starts `mcp-proton serve --transport stdio` as a subprocess. The process runs as your user and stops when the client exits.

Stdio clients inherit your OS user's trust. The `--client-id` value (sent as `MCP_PROTON_CLIENT_ID`) lets policy tell clients apart for organization. **It is not authentication.** Any other process running as the same user can send the same identifier.

## Isolated service

Use this model when you want policy, credentials and approvals to sit outside the agent's permissions. The setup below is a checklist, not a tested reference. Check each point on your own system. The design says the boundary must be verified in the actual deployment (see `docs/design.md`).

### Users and files

1. Create a separate OS user for the service, for example `mcp-proton`. The agent must not run as this user.
2. Place the configuration and policy files in that user's config directory. Make them owned by the service user and not writable by the agent user. `config.toml` and `policy.toml` hold settings and owner policy, not secrets.
3. Keep the data directory (the SQLite database and managed artifacts) owned by the service user. The agent user should have no read access to it.
4. Store Bridge credentials in the service user's keyring. Do not store them in a file the agent can read. If you use the `file:` secret provider, the file must be mode `0600` and owned by the service user.
5. Keep the owner approval interface (the CLI or a future UI) under the owner's account, not under the agent's.

Separate users are not enough if the agent can still read Bridge credentials, reach an unprotected admin endpoint or gain the service user's privileges. Check each of these.

### Network

- Bind HTTP to loopback. The default is `127.0.0.1:8765`. Do not bind to `0.0.0.0` on a shared host.
- To reach the service from another machine, put it behind a transport you control, such as an SSH tunnel or a VPN. Do not expose the port directly to the internet.
- Set `allowed_origins` in `config.toml` to the browser origins that may call the HTTP interface. Leave it empty unless you have a browser client.

### Client tokens

Create one bearer token per client, so each client can be revoked separately:

```sh
mcp-proton clients add hermes --http-token
mcp-proton clients list
mcp-proton clients revoke hermes
```

`clients add --http-token` creates the bearer token. Check the command output for how the token is shown. Store it only in the client's own configuration, with restricted file permissions. Each client gets its own permissions in policy. Revoke a client as soon as it is no longer needed.

Print a client configuration that uses HTTP with:

```sh
mcp-proton client-config hermes --client-id hermes --http <service URL>
```

Pass the URL of the running service. The output contains a token placeholder. Replace it with the client's token by hand. This document does not state whether the HTTP transport terminates TLS itself or what endpoint path it serves. Check the server output. Do not send a bearer token over plain HTTP across a network. Use a TLS-terminating tunnel or VPN.

### Owner-only administration

Owner commands (`policy`, `clients`, `approvals`, `pause`, `purge`) run as the owner through the CLI. Owner administration is not exposed through the MCP tools or the client bearer verifier. Keep the owner credential away from agents. The local review UI uses a separate owner token, created with `mcp-proton ui-token`. The token is shown once. Only its SHA-256 hash is stored (`owner_token_sha256`).

### Approvals

Ask decisions are made with the owner CLI:

```sh
mcp-proton approvals list
mcp-proton approvals show <operation_id>
mcp-proton approvals approve <operation_id> --execute
mcp-proton approvals deny <operation_id>
```

An approval binds to the exact payload, account and expiry. A client cannot approve its own request by sending `confirmed=true`. The client label does not grant approval authority.

### Service mode for schedules

Schedules, reminders and continuous watching (Phase 4 and Phase 2 change tracking) need an always-running process. A stdio subprocess stops when its client exits. For these features, run the service as its own process under a supervisor. This example is a systemd user unit for the service user. It is untested in this repository.

```ini
[Unit]
Description=mcp-proton service
After=network-online.target

[Service]
ExecStart=/path/to/venv/bin/mcp-proton serve --transport http
Restart=on-failure
NoNewPrivileges=yes

[Install]
WantedBy=default.target
```

Use the service user's account for this unit, not the agent's. Check that the process restarts after Bridge restarts. Reconnection and capability rediscovery are the adapter's job, and their behaviour is not verified against Bridge.

## Hermes integration

The Hermes client can connect through stdio:

```sh
mcp-proton client-config hermes --client-id hermes
```

The generated configuration uses an `mcp_servers` block with a `command`, `args` and an `env` entry for the client identifier. **Verify the exact schema against your installed Hermes version before use.** The generated text is marked as an example. This repository does not verify Hermes's negotiated protocol features, approval handling or timeout behaviour against a live Hermes install.

## Local review UI

The CLI includes a local owner review UI command:

```sh
mcp-proton ui-token
mcp-proton ui --host 127.0.0.1 --port 8766
```

The command binds to loopback by default. The `ui` command imports a module (`admin/ui.py`) that was not present in the code reviewed for this guide. If the module is missing, the command reports that the review UI is not available in this install. Check your installed version before relying on the UI. Until the UI is verified, use the CLI for approvals.

The UI needs authentication, owner-only administration, and origin and CSRF checks, as the design requires (see `docs/design.md`). Confirm these in the version you run.

## Elicitation review over stdio

`mcp-proton clients review-channel <id> elicitation` makes the server ask the connecting client to approve its own pending operations. Over stdio the client id is only a label: the server cannot tell which process supplied it. **Setting `elicitation` for a stdio client id therefore trusts every local process that can launch the server with that label** to approve requests as that client. In an isolated deployment, use `elicitation` only for authenticated HTTP clients (bound to a token); keep stdio clients on the owner `queue`.

## Summary of guarantees

- Personal local: policy governs mcp-proton calls only. It does not stop an agent with shell access in the same OS account.
- Isolated service: the boundary holds only if the OS users, file ownership, network binding and token handling are set up and checked as above.
- No deployment makes the content of an email safe for an agent to act on. Treat email as untrusted input.
