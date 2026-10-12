"""Setup reads Bridge's certificate over STARTTLS or implicit TLS (connection mode SSL)."""

from __future__ import annotations

import datetime
import hashlib
import socket
import ssl
import threading

import pytest

from mcp_proton.bridge import tls
from mcp_proton.config import Security

x509 = pytest.importorskip("cryptography.x509")


@pytest.fixture
def cert(tmp_path):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    c = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
         .public_key(key.public_key()).serial_number(1)
         .not_valid_before(now - datetime.timedelta(days=1))
         .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "c.pem", tmp_path / "k.pem"
    cert_path.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    der = c.public_bytes(serialization.Encoding.DER)
    return cert_path, key_path, hashlib.sha256(der).hexdigest()


def _serve_once(cert_path, key_path, starttls: bool) -> int:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    srv = socket.create_server(("127.0.0.1", 0))

    def run():
        conn, _ = srv.accept()
        with conn:
            if starttls:
                conn.sendall(b"* OK ready\r\n")
                conn.recv(1024)
                conn.sendall(b"a1 OK begin TLS\r\n")
            try:
                with ctx.wrap_socket(conn, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                pass
        srv.close()

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


@pytest.mark.parametrize("security", [Security.STARTTLS, Security.SSL])
def test_fingerprint_is_read_in_both_connection_modes(cert, security):
    cert_path, key_path, expected = cert
    port = _serve_once(cert_path, key_path, starttls=security is Security.STARTTLS)
    assert tls.fetch_fingerprint_imap("127.0.0.1", port, security, timeout=5) == expected
