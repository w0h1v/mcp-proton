"""SMTP submission through Proton Bridge (stdlib ``smtplib``), one connection per send.

Outcome contract of :meth:`SmtpTransport.send` (never retries internally):

* Failure before DATA (connect, TLS, auth, MAIL FROM, a dropped RCPT reply) means
  nothing was submitted: network errors raise ``SmtpSendError`` (safe to retry);
  TLS/auth problems raise ``MailError`` (``TLS_ERROR`` / ``AUTH_FAILED``).
* Every recipient rejected: results returned with ``accepted=False``; DATA is
  never issued.
* DATA refused (non-354) or final reply not 2xx: results returned with
  ``accepted=False`` and the server code.
* Connection loss, timeout or any unexpected error once the DATA command was
  issued, including a missing final reply: ``SmtpDeliveryUnknown`` carrying the
  RCPT-stage results. The message may or may not have been accepted.
* Final reply 2xx: results with the RCPT-stage acceptance.

TLS: STARTTLS or implicit SSL with the pin from ``bridge.tls``; plaintext only
on loopback (``Security.NONE``, where a failed/absent AUTH is tolerated for test
servers). Message content and addresses are never logged.
"""

from __future__ import annotations

import re
import smtplib
import ssl
from collections.abc import Callable

from ..config import AccountConfig, Security
from ..domain.errors import ErrorCode, MailError
from ..domain.models import RecipientResult
from . import tls
from .credentials import resolve_secret
from .ports import SmtpDeliveryUnknown, SmtpSendError

_LOCAL_HOSTNAME = "localhost"  # avoid leaking/resolving the machine's FQDN in EHLO
_CRLF = re.compile(rb"\r\n|\n|\r")


def _reply_text(resp: bytes | str | None) -> str:
    text = resp.decode("utf-8", "replace") if isinstance(resp, bytes) else str(resp or "")
    return text.replace("\r", " ").replace("\n", " ")[:200]


class SmtpTransport:
    """Implements ``ports.MailTransport``. Stateless between calls; thread-safe."""

    def __init__(
        self,
        account: AccountConfig,
        password_provider: Callable[[], str] | None = None,
    ) -> None:
        self._cfg = account
        self.account = account.name
        self._password = password_provider or (lambda: resolve_secret(account.secret_ref))

    # ------------------------------------------------------------ connection

    def _open(self) -> smtplib.SMTP:
        """Connected, TLS-verified, authenticated session. Raises ``MailError`` only."""
        cfg = self._cfg
        tls.require_transport_security(cfg.smtp_host, cfg.smtp_security)
        smtp: smtplib.SMTP | None = None
        try:
            if cfg.smtp_security is Security.SSL:
                smtp = smtplib.SMTP_SSL(
                    cfg.smtp_host,
                    cfg.smtp_port,
                    local_hostname=_LOCAL_HOSTNAME,
                    timeout=cfg.connect_timeout,
                    context=tls.make_ssl_context(cfg),
                )
            else:
                smtp = smtplib.SMTP(
                    cfg.smtp_host,
                    cfg.smtp_port,
                    local_hostname=_LOCAL_HOSTNAME,
                    timeout=cfg.connect_timeout,
                )
            smtp.ehlo()
            if cfg.smtp_security is Security.STARTTLS:
                if not smtp.has_extn("starttls"):
                    raise MailError(ErrorCode.TLS_ERROR, "server does not offer STARTTLS")
                smtp.starttls(context=tls.make_ssl_context(cfg))
                smtp.ehlo()
            if cfg.smtp_security is not Security.NONE:
                tls.verify_peer(cfg, smtp.sock)
            if smtp.has_extn("auth"):
                try:
                    smtp.login(cfg.username, self._password())
                except smtplib.SMTPAuthenticationError:
                    # Plaintext loopback is a test-server mode: such servers may advertise AUTH
                    # without accepting any login. A server that really needs auth will refuse
                    # MAIL FROM below. Over TLS (real Bridge) a failed login is always fatal.
                    if cfg.smtp_security is not Security.NONE:
                        raise
            elif cfg.smtp_security is not Security.NONE:
                raise MailError(ErrorCode.AUTH_FAILED, "server does not offer authentication")
            return smtp
        except MailError:
            _close(smtp)
            raise
        except smtplib.SMTPAuthenticationError as exc:
            _close(smtp)
            raise MailError(ErrorCode.AUTH_FAILED, "SMTP authentication failed") from exc
        except ssl.SSLError as exc:
            _close(smtp)
            raise MailError(ErrorCode.TLS_ERROR, "TLS handshake with Bridge failed") from exc
        except (OSError, smtplib.SMTPException) as exc:
            _close(smtp)
            raise MailError(
                ErrorCode.BRIDGE_UNAVAILABLE, f"cannot reach Bridge SMTP ({type(exc).__name__})"
            ) from exc

    # ------------------------------------------------------------ MailTransport

    def check(self) -> None:
        """Diagnostics: connect, EHLO, TLS/auth, NOOP, QUIT. Raises ``MailError``."""
        smtp = self._open()
        try:
            code, _ = smtp.noop()
            if code != 250:
                raise MailError(ErrorCode.BRIDGE_UNAVAILABLE, f"SMTP NOOP returned {code}")
        except (OSError, smtplib.SMTPException) as exc:
            raise MailError(ErrorCode.BRIDGE_UNAVAILABLE, "SMTP session failed") from exc
        finally:
            _close(smtp)

    def send(self, envelope_from: str, recipients: list[str], raw: bytes) -> list[RecipientResult]:
        try:
            smtp = self._open()
        except MailError as exc:
            if exc.code is ErrorCode.BRIDGE_UNAVAILABLE:
                raise SmtpSendError(exc.message) from exc
            raise

        results: list[RecipientResult] = []
        data_issued = False
        try:
            code, resp = smtp.mail(envelope_from)
            if code != 250:
                raise SmtpSendError(f"MAIL FROM rejected ({code})")
            for rcpt in recipients:
                code, resp = smtp.rcpt(rcpt)
                results.append(
                    RecipientResult(
                        email=rcpt,
                        accepted=code in (250, 251),
                        code=code,
                        message=_reply_text(resp),
                    )
                )
            if not any(r.accepted for r in results):
                _close(smtp)
                return results
            data_issued = True
            code, resp = smtp.data(_CRLF.sub(b"\r\n", raw))
        except SmtpSendError:
            _close(smtp, quit_=False)
            raise
        except smtplib.SMTPDataError as exc:  # DATA refused with 4xx/5xx before any content
            _close(smtp)
            return _all_rejected(results, exc.smtp_code, exc.smtp_error)
        except Exception as exc:  # noqa: BLE001 - classification below is the whole point
            _close(smtp, quit_=False)
            if data_issued:
                raise SmtpDeliveryUnknown(
                    f"connection failed after DATA ({type(exc).__name__}); delivery unknown",
                    results,
                ) from exc
            raise SmtpSendError(f"SMTP failure before DATA ({type(exc).__name__})") from exc

        _close(smtp)
        if 200 <= code < 300:
            return results
        return _all_rejected(results, code, resp)


def _all_rejected(
    results: list[RecipientResult], code: int, resp: bytes | str | None
) -> list[RecipientResult]:
    text = _reply_text(resp)
    return [
        RecipientResult(email=r.email, accepted=False, code=code, message=text) for r in results
    ]


def _close(smtp: smtplib.SMTP | None, quit_: bool = True) -> None:
    if smtp is None:
        return
    try:
        if quit_:
            smtp.quit()
        else:
            smtp.close()
    except (OSError, smtplib.SMTPException):
        smtp.close()


def open_transport(account: AccountConfig) -> SmtpTransport:
    return SmtpTransport(account)
