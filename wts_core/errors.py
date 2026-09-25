"""Service-level errors shared by every core query.

Mirrors ``src-tauri/src/error.rs``. The only structured bit of information the
core exposes is whether a failure means "the school session expired", which
drives the single bounded re-login retry in :mod:`wts_core.session_cache`.
"""

from __future__ import annotations


class ServiceError(Exception):
    """A user-facing failure raised by a core service."""

    def __init__(self, message: str, *, authentication_expired: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.authentication_expired = authentication_expired

    @classmethod
    def with_status(cls, message: str, status_code: int) -> "ServiceError":
        return cls(message, authentication_expired=status_code == 401)

    @classmethod
    def expired(cls) -> "ServiceError":
        return cls.with_status("登录状态已失效，请重新获取。", 401)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message
