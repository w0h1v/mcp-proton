"""Resolve the :class:`CallerContext` for every tool, resource and prompt.

* **stdio**: the client id is a *label* from ``MCP_PROTON_CLIENT_ID`` (default
  ``default``); the process inherits the OS user's trust, so the caller is not
  marked authenticated.
* **HTTP**: the bearer token was verified by :mod:`.auth` before the request
  reached us; the client id comes from the token, never from the MCP
  ``clientInfo`` the peer reports about itself. Policy is consulted again here
  so a revoked client or a rotated token is refused even on a live session.
"""

from __future__ import annotations

import os
import re

from fastmcp.server.dependencies import get_access_token

from ..domain.errors import ErrorCode, MailError
from ..domain.requests import CallerContext, Transport
from ..policy.model import ClientConfig
from ..services.core import MailApp

ENV_CLIENT_ID = "MCP_PROTON_CLIENT_ID"
DEFAULT_CLIENT_ID = "default"
_CLIENT_ID = re.compile(r"^[A-Za-z0-9_.@:-]{1,64}$")


def stdio_client_id() -> str:
    raw = os.environ.get(ENV_CLIENT_ID, "").strip() or DEFAULT_CLIENT_ID
    if not _CLIENT_ID.match(raw):
        raise MailError(ErrorCode.INVALID_REQUEST, f"{ENV_CLIENT_ID} is not a valid client id")
    return raw


class Identity:
    def __init__(self, app: MailApp, transport: str) -> None:
        if transport not in ("stdio", "http"):
            raise ValueError(f"unsupported transport {transport!r}")
        self.app = app
        self.transport = Transport(transport)

    def caller(self) -> CallerContext:
        if self.transport is Transport.STDIO:
            return CallerContext(client_id=stdio_client_id(), transport=Transport.STDIO,
                                 authenticated=False)
        token = get_access_token()
        if token is None or not token.client_id:
            raise MailError(ErrorCode.AUTH_FAILED, "authentication required")
        client = self.app.policy.client(token.client_id)
        if client is None:
            raise MailError(ErrorCode.AUTH_FAILED, "unknown client")
        if client.revoked:
            raise MailError(ErrorCode.CLIENT_REVOKED, "this client has been revoked")
        # A token rotated after this request was authenticated no longer counts.
        if client.token_sha256 and token.claims.get("token_sha256") not in (
            None, client.token_sha256.lower()
        ):
            raise MailError(ErrorCode.AUTH_FAILED, "token is no longer valid")
        return CallerContext(client_id=client.client_id, transport=Transport.HTTP,
                             authenticated=True)

    def client_config(self, caller: CallerContext) -> ClientConfig | None:
        return self.app.policy.client(caller.client_id)
