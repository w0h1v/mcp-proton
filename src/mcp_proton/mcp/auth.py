"""HTTP bearer authentication against the owner's policy clients.

A bearer token is hashed (SHA-256) and compared in constant time with every
``ClientConfig.token_sha256``. The policy is read through a callable on each
request (``MailApp.policy`` re-reads ``policy.toml`` when it changes), so a
revocation or token rotation takes effect on the very next request. The raw
token is never stored, logged, or placed in the access token object.

Owner administration (policy edits, approvals) is not reachable through this
verifier or any MCP surface: it belongs to the CLI and the local review UI.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Iterable

from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from ..domain.errors import ErrorCode, MailError
from ..policy.model import ClientConfig, PolicyConfig

PolicySource = Callable[[], PolicyConfig]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()


def match_client(policy: PolicyConfig, token: str) -> ClientConfig | None:
    """The non-revoked client whose token hash matches ``token``, in constant time.

    Every token-bearing client is compared (no early exit) so timing does not
    reveal which prefix matched.
    """
    digest = hash_token(token)
    found: ClientConfig | None = None
    for client in policy.clients:
        if not client.token_sha256:
            continue
        if hmac.compare_digest(digest.encode(), client.token_sha256.lower().encode()):
            found = client
    if found is None or found.revoked:
        return None
    return found


def token_clients(policy: PolicyConfig) -> list[ClientConfig]:
    """Clients that can authenticate over HTTP (token set and not revoked)."""
    return [c for c in policy.clients if c.token_sha256 and not c.revoked]


def require_http_clients(policy: PolicyConfig) -> None:
    """Refuse to serve HTTP when nobody could authenticate."""
    if not token_clients(policy):
        raise MailError(
            ErrorCode.INVALID_REQUEST,
            "refusing to start the HTTP transport: no client with a bearer token is "
            "configured (create one with `mcp-proton clients add <id> --http-token`)",
        )


class ProtonTokenVerifier(TokenVerifier):
    """FastMCP token verifier backed by ``policy.clients``."""

    def __init__(self, policy_source: PolicySource) -> None:
        super().__init__()
        self._policy_source = policy_source

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            client = match_client(self._policy_source(), token)
        except Exception:  # noqa: BLE001 - fail closed on any policy problem
            return None
        if client is None:
            return None
        digest = hash_token(token)
        return AccessToken(
            token=digest,  # never keep the raw bearer token around
            client_id=client.client_id,
            scopes=[],
            claims={"client_id": client.client_id, "token_sha256": digest},
        )


def _normalize_origin(origin: str) -> str:
    return origin.strip().rstrip("/").lower()


class OriginGuardMiddleware:
    """Reject requests that carry a browser ``Origin`` not on the owner's list.

    Non-browser MCP clients send no ``Origin`` header and pass untouched. There
    is deliberately no same-origin or localhost fallback: a web page on another
    local port is still a cross-origin caller. Configure
    ``[http] allowed_origins`` to admit a specific browser-based client.
    """

    def __init__(self, app: ASGIApp, allowed_origins: Iterable[str] = ()) -> None:
        self.app = app
        self.allowed = frozenset(_normalize_origin(o) for o in allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            origin = Headers(scope=scope).get("origin")
            if origin is not None and _normalize_origin(origin) not in self.allowed:
                await Response("Forbidden Origin", status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)
