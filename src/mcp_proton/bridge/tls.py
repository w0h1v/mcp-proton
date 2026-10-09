"""TLS trust for Proton Bridge's self-signed certificate.

Trust is configured during setup and verified on every connection:

* ``tls_ca_file``: an exported Bridge certificate used as the only trust root
  (hostname checking disabled because Bridge certs name 127.0.0.1 inconsistently).
* ``tls_fingerprint_sha256``: the leaf certificate is pinned by SHA-256; the
  chain is not validated, the pin is. Mismatch raises ``tls_error``.

Never silently disables verification. ``Security.NONE`` is refused for
non-loopback hosts.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import ssl

from ..config import AccountConfig, Security
from ..domain.errors import ErrorCode, MailError


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_transport_security(host: str, security: Security) -> None:
    if security is Security.NONE and not is_loopback(host):
        raise MailError(ErrorCode.TLS_ERROR, "plaintext connections are only allowed to loopback")


def normalize_fingerprint(fp: str) -> str:
    return fp.replace(":", "").replace(" ", "").lower()


def make_ssl_context(account: AccountConfig) -> ssl.SSLContext:
    """Context for STARTTLS/SSL. With a fingerprint pin, verification happens in
    ``verify_peer`` after the handshake."""
    if account.tls_ca_file:
        ctx = ssl.create_default_context(cafile=account.tls_ca_file)
        ctx.check_hostname = False
        return ctx
    if account.tls_fingerprint_sha256:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # pin is enforced by verify_peer()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx
    return ssl.create_default_context()


def peer_fingerprint(sock: ssl.SSLSocket) -> str:
    der = sock.getpeercert(binary_form=True)
    if not der:
        raise MailError(ErrorCode.TLS_ERROR, "server presented no certificate")
    return hashlib.sha256(der).hexdigest()


def verify_peer(account: AccountConfig, sock: object) -> None:
    """Enforce the fingerprint pin on an established TLS socket."""
    if not account.tls_fingerprint_sha256:
        return
    if not isinstance(sock, ssl.SSLSocket):
        raise MailError(ErrorCode.TLS_ERROR, "expected a TLS connection")
    got = peer_fingerprint(sock)
    if got != normalize_fingerprint(account.tls_fingerprint_sha256):
        raise MailError(ErrorCode.TLS_ERROR, "Bridge certificate fingerprint mismatch",
                        expected=normalize_fingerprint(account.tls_fingerprint_sha256)[:16] + "...")


def fetch_fingerprint_starttls_imap(host: str, port: int, timeout: float = 10.0) -> str:
    """Setup helper: read Bridge's certificate fingerprint via IMAP STARTTLS so the
    owner can confirm and pin it (trust on first use, shown to the owner)."""
    with socket.create_connection((host, port), timeout=timeout) as raw:
        f = raw.makefile("rb")
        f.readline()  # greeting
        raw.sendall(b"a1 STARTTLS\r\n")
        line = f.readline()
        if not line.startswith(b"a1 OK"):
            raise MailError(ErrorCode.TLS_ERROR, "server refused STARTTLS")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # only to read the cert for owner confirmation
        with ctx.wrap_socket(raw, server_hostname=host) as tls:
            return peer_fingerprint(tls)
