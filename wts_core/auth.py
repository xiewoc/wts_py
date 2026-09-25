"""Credential resolution shared by every authenticated query.

Mirrors ``src-tauri/src/auth.rs``: an explicit argument always wins, otherwise
the terminal clients fall back to ``BUPT_USERNAME`` / ``BUPT_PASSWORD``.
"""

from __future__ import annotations

import os

from .errors import ServiceError

MISSING_CREDENTIALS_MESSAGE = (
    "请填写学号和教务密码，或在环境变量中配置 BUPT_USERNAME/BUPT_PASSWORD。"
)


def resolve_credentials(
    account: str | None, password: str | None
) -> tuple[str, str]:
    """Return ``(account, password)`` or raise a 400 ``ServiceError``."""
    user = (account or "").strip()
    if not user:
        user = os.environ.get("BUPT_USERNAME", "")

    secret = password if password else os.environ.get("BUPT_PASSWORD", "")

    user = user.strip()
    if not user or not secret:
        raise ServiceError.with_status(MISSING_CREDENTIALS_MESSAGE, 400)
    return user, secret
