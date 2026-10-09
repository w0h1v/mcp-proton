"""Secret resolution. References only; no silent plaintext fallback.

* ``keyring:<service>/<username>`` — OS credential store (default).
* ``env:<VAR>`` — environment variable (explicit headless provider).
* ``file:<path>`` — file readable only by the owner (mode 0600 enforced).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from ..domain.errors import ErrorCode, MailError

KEYRING_SERVICE = "mcp-proton"


def resolve_secret(ref: str) -> str:
    scheme, _, rest = ref.partition(":")
    if not rest:
        raise MailError(ErrorCode.INVALID_REQUEST, "secret reference must be scheme:value")
    if scheme == "keyring":
        import keyring

        service, _, user = rest.partition("/")
        if not user:
            service, user = KEYRING_SERVICE, rest
        value = keyring.get_password(service, user)
        if value is None:
            raise MailError(ErrorCode.AUTH_FAILED, "secret not found in OS credential store")
        return value
    if scheme == "env":
        value = os.environ.get(rest)
        if value is None:
            raise MailError(ErrorCode.AUTH_FAILED, f"environment variable {rest} is not set")
        return value
    if scheme == "file":
        p = Path(rest).expanduser()
        try:
            mode = p.stat().st_mode
        except OSError as e:
            raise MailError(ErrorCode.AUTH_FAILED, "secret file not readable") from e
        if os.name == "posix" and mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise MailError(ErrorCode.AUTH_FAILED, "secret file must not be group/world accessible")
        return p.read_text().strip()
    raise MailError(ErrorCode.INVALID_REQUEST, f"unknown secret provider {scheme!r}")


def store_secret_keyring(username: str, secret: str, service: str = KEYRING_SERVICE) -> str:
    import keyring

    keyring.set_password(service, username, secret)
    return f"keyring:{service}/{username}"
