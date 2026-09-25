"""Opaque local account scopes and their scoped JSON envelope.

Mirrors ``src-tauri/src/scoped_cache.rs``: a 32-byte random scope keeps caches
bound to one local account without ever storing the account name, and a legacy
unscoped payload is reported as "no data" instead of being adopted.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from .errors import ServiceError

SCHEMA_VERSION = 1
SCOPE_PREFIX = "opaque-v1:"
SCOPE_BYTES = 32


def new_account_scope() -> str:
    return SCOPE_PREFIX + secrets.token_hex(SCOPE_BYTES)


def is_valid_account_scope(scope: str) -> bool:
    return _validate_scope(scope) is None


def _validate_scope(scope: str) -> str | None:
    if not scope.startswith(SCOPE_PREFIX):
        return "账号作用域格式无效"
    digest = scope[len(SCOPE_PREFIX) :]
    if len(digest) != SCOPE_BYTES * 2 or any(
        character not in "0123456789abcdefABCDEF" for character in digest
    ):
        return "账号作用域格式无效"
    return None


def encode(scope: str, payload: Any, description: str) -> bytes:
    message = _validate_scope(scope)
    if message is not None:
        raise ServiceError(f"无法保存{description}：{message}")
    document = {
        "schema_version": SCHEMA_VERSION,
        "account_scope": scope,
        # ``payload`` is already a list of ``to_dict()`` results, not models.
        "payload": payload,
    }
    try:
        return json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
    except (TypeError, ValueError) as error:  # pragma: no cover - defensive
        raise ServiceError(f"无法序列化{description}：{error}") from error


def decode(
    raw: bytes, expected_scope: str, description: str, payload_type: type
) -> Any | None:
    """Return the decoded payload, or ``None`` when the cache does not belong here."""
    message = _validate_scope(expected_scope)
    if message is not None:
        raise ServiceError(f"无法读取{description}：{message}")
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise ServiceError(f"{description}格式不正确：{error}") from error
    if not isinstance(document, dict):
        raise ServiceError(f"{description}格式不正确。")
    if "schema_version" not in document and "account_scope" not in document:
        return None
    stored_scope = document.get("account_scope")
    if not isinstance(stored_scope, str) or _validate_scope(stored_scope) is not None:
        raise ServiceError(f"{description}格式不正确：账号作用域格式无效")
    if document.get("schema_version") != SCHEMA_VERSION or stored_scope != expected_scope:
        return None
    try:
        return payload_type.from_dict(document.get("payload"))
    except (TypeError, ValueError, KeyError) as error:
        raise ServiceError(f"{description}格式不正确：{error}") from error
