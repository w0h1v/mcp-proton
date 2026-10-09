"""Client configuration snippets for MCP clients.

Snippets are examples: the stdio client id is an organizational label (not
authentication); HTTP uses a bearer token created with ``clients add``.
The Hermes shape follows its documented ``mcp_servers`` mapping but must be
verified against the installed Hermes version.
"""

from __future__ import annotations

import json

KINDS = ("hermes", "claude-desktop", "generic")
SERVER_NAME = "proton"
TOKEN_PLACEHOLDER = "<TOKEN from `mcp-proton clients add --http-token`>"  # noqa: S105


def _j(value: object) -> str:
    return json.dumps(value)


def render(kind: str, client_id: str = "default", http_url: str | None = None,
           command: str = "mcp-proton") -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown client kind {kind!r}")
    if kind == "hermes":
        return _hermes(client_id, http_url, command)
    if http_url:
        entry: dict[str, object] = {"url": http_url,
                                    "headers": {"Authorization": f"Bearer {TOKEN_PLACEHOLDER}"}}
    else:
        entry = {"command": command, "args": ["serve", "--transport", "stdio"],
                 "env": {"MCP_PROTON_CLIENT_ID": client_id}}
    if kind == "claude-desktop":
        return json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2)
    return json.dumps({"name": SERVER_NAME, "transport": "http" if http_url else "stdio",
                       **entry}, indent=2)


def _hermes(client_id: str, http_url: str | None, command: str) -> str:
    lines = ["# Example only: verify the exact schema against your Hermes version.",
             "mcp_servers:", f"  {SERVER_NAME}:"]
    if http_url:
        lines += [f"    url: {_j(http_url)}", "    headers:",
                  f"      Authorization: {_j('Bearer ' + TOKEN_PLACEHOLDER)}"]
    else:
        lines += [f"    command: {_j(command)}",
                  f"    args: {_j(['serve', '--transport', 'stdio'])}",
                  "    env:", f"      MCP_PROTON_CLIENT_ID: {_j(client_id)}"]
    return "\n".join(lines)
