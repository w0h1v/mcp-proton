"""Bounded, machine-readable errors.

Every error that crosses an entry point (MCP tool, CLI, UI) is a ``MailError``
with a stable ``code``. Messages never include credentials or message bodies.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    NOT_ONBOARDED = "not_onboarded"
    POLICY_DENIED = "policy_denied"
    CONSTRAINT_VIOLATION = "constraint_violation"
    LIMIT_EXCEEDED = "limit_exceeded"
    NOT_FOUND = "not_found"
    STALE_HANDLE = "stale_handle"
    CONFLICT = "conflict"
    UNSUPPORTED = "unsupported"
    UNSUPPORTED_SEMANTICS = "unsupported_semantics"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    BRIDGE_UNAVAILABLE = "bridge_unavailable"
    AUTH_FAILED = "auth_failed"
    TLS_ERROR = "tls_error"
    DELIVERY_UNKNOWN = "delivery_unknown"
    APPROVAL_INVALID = "approval_invalid"
    EXPIRED = "expired"
    ACCOUNT_PAUSED = "account_paused"
    CLIENT_REVOKED = "client_revoked"
    PATH_REJECTED = "path_rejected"
    TOO_LARGE = "too_large"
    INTERNAL = "internal"


class MailError(Exception):
    """Base error with a stable code and bounded, non-sensitive details."""

    def __init__(self, code: ErrorCode, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": str(self.code), "message": self.message, "details": self.details}

    def __repr__(self) -> str:
        return f"MailError({self.code!s}, {self.message!r})"


def invalid(message: str, **details: Any) -> MailError:
    return MailError(ErrorCode.INVALID_REQUEST, message, **details)


def not_found(message: str, **details: Any) -> MailError:
    return MailError(ErrorCode.NOT_FOUND, message, **details)


def stale(message: str, **details: Any) -> MailError:
    return MailError(ErrorCode.STALE_HANDLE, message, **details)
