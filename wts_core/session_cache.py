"""Process-local, credential-scoped sessions.

Mirrors ``src-tauri/src/session_cache.rs``: no token and no credential hash is
ever written to disk, the login path (never the authenticated request) is
serialised, and an authenticated failure triggers exactly one bounded re-login.
The epoch captured with the caller's credentials prevents a revoked or
account-switched request from adopting a newer session.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .errors import ServiceError

MAX_TOKEN_SEGMENT_LENGTH = 32_768


class SessionEpoch(int):
    """Opaque generation marker captured before any ``await``."""


@dataclass
class _Entry:
    key: bytes
    id: int
    deadline: float
    value: Any


class SessionCache:
    """A single-slot session cache keyed by the credential digest."""

    def __init__(self) -> None:
        self._generation = 0
        self._sequence = 0
        self._entry: _Entry | None = None
        self._login_lock = asyncio.Lock()

    def clear(self) -> None:
        self._generation += 1
        self._entry = None

    def epoch(self) -> SessionEpoch:
        return SessionEpoch(self._generation)

    def _invalidate(self, entry_id: int) -> None:
        if self._entry is not None and self._entry.id == entry_id:
            self._entry = None

    def _current(self, generation: int) -> None:
        if self._generation != generation:
            raise ServiceError("账户已更改，请重新获取。")

    async def _get(
        self,
        key: bytes,
        generation: int,
        login: Callable[[], Awaitable[tuple[Any, float]]],
    ) -> tuple[int, Any]:
        # Serialize only the login path, not authenticated HTTP requests.
        async with self._login_lock:
            self._current(generation)
            entry = self._entry
            if entry is not None and entry.key == key and entry.deadline > time.monotonic():
                return entry.id, entry.value
            value, ttl = await login()
            self._current(generation)
            self._sequence += 1
            entry_id = self._sequence
            self._entry = _Entry(
                key=key, id=entry_id, deadline=time.monotonic() + ttl, value=value
            )
            return entry_id, value

    async def run(
        self,
        account: str,
        password: str,
        login: Callable[[], Awaitable[tuple[Any, float]]],
        request: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        """Run ``request`` with a token for these credentials.

        The epoch is captured before the first ``await`` of this coroutine, so a
        session cleared between creating and awaiting it can never be reused.
        Account-managed callers should prefer :meth:`run_at` with the epoch
        captured next to their credentials.
        """
        return await self.run_at(self.epoch(), account, password, login, request)

    async def run_at(
        self,
        epoch: SessionEpoch,
        account: str,
        password: str,
        login: Callable[[], Awaitable[tuple[Any, float]]],
        request: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        generation = int(epoch)
        self._current(generation)
        key = credential_key(account, password)
        entry_id, value = await self._get(key, generation, login)
        self._current(generation)
        try:
            result = await request(value)
        except ServiceError as error:
            if not error.authentication_expired:
                raise
            # An older failed request must not evict a newer session.
            self._invalidate(entry_id)
            retry_id, value = await self._get(key, generation, login)
            self._current(generation)
            try:
                result = await request(value)
            except ServiceError as retry_error:
                if retry_error.authentication_expired:
                    self._invalidate(retry_id)
                raise
            self._current(generation)
            return result
        self._current(generation)
        return result


def credential_key(account: str, password: str) -> bytes:
    hasher = hashlib.sha256()
    hasher.update(len(account).to_bytes(8, "big"))
    hasher.update(account.encode("utf-8"))
    hasher.update(len(password).to_bytes(8, "big"))
    hasher.update(password.encode("utf-8"))
    return hasher.digest()


AUTH_EXPIRED_MARKERS = (
    "token expired",
    "invalid token",
    "token失效",
    "token过期",
    "登录已过期",
    "登录超时",
    "请先登录",
    "未登录",
    "登录失效",
)


def check_auth_payload(payload: Any) -> None:
    """Recognize explicit authentication responses, never arbitrary failures."""
    code: int | None = None
    if isinstance(payload, dict):
        raw_code = payload.get("code")
        if isinstance(raw_code, bool):
            code = None
        elif isinstance(raw_code, int):
            code = raw_code
        elif isinstance(raw_code, str):
            try:
                code = int(raw_code)
            except ValueError:
                code = None
    if code is not None and (code in (403, 423) or 500 <= code <= 599):
        return

    message = ""
    if isinstance(payload, dict):
        for key in ("msg", "Msg", "message"):
            value = payload.get(key)
            if isinstance(value, str):
                message = value
                break
    message = message.lower()
    if code == 401 or any(marker in message for marker in AUTH_EXPIRED_MARKERS):
        raise ServiceError.expired()


def session_ttl(seconds: int | None) -> float:
    """Bound a session lifetime; without metadata use a conservative 20 minutes."""
    value = 20 * 60 if seconds is None else max(0, int(seconds))
    value = min(value, 7 * 24 * 60 * 60)
    return float(value - min(value, 30))


def token_ttl(token: str, declared: int | None) -> float:
    """Combine a declared lifetime with the JWT ``exp`` hint, whichever is smaller."""
    jwt_seconds = _jwt_expiry_seconds(token)
    if declared is not None and jwt_seconds is not None:
        return session_ttl(min(declared, jwt_seconds))
    return session_ttl(declared if declared is not None else jwt_seconds)


def _jwt_expiry_seconds(token: str) -> int | None:
    segments = token.split(".")
    if len(segments) < 2:
        return None
    segment = segments[1]
    if not segment or len(segment) > MAX_TOKEN_SEGMENT_LENGTH:
        return None
    padded = segment.rstrip("=")
    padded += "=" * (-len(padded) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    expiry = payload.get("exp")
    if not isinstance(expiry, int) or isinstance(expiry, bool):
        return None
    return max(0, expiry - int(time.time()))

