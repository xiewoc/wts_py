"""Credential payload shared with the file-backed terminal clients.

Mirrors ``where-to-study-core/src/credential_store.rs``. Platform-specific
secure storage stays owned by each application target; this model only defines
the payload and the teaching-cloud password fallback rule.
"""

from __future__ import annotations

from dataclasses import dataclass

from .json_model import JsonModel


@dataclass
class Credentials(JsonModel):
    account: str = ""
    password: str = ""
    teaching_cloud_password: str | None = None
    account_scope: str = ""

    omit_when_none = frozenset({"teaching_cloud_password"})

    def assignment_password(self) -> str:
        """Teaching-cloud password, falling back to the academic password."""
        override = self.teaching_cloud_password
        if override:
            return override
        return self.password

    def __repr__(self) -> str:
        return (
            "Credentials(has_account={has_account}, has_password={has_password}, "
            "has_teaching_cloud_password={has_teaching_cloud_password})"
        ).format(
            has_account=bool(self.account),
            has_password=bool(self.password),
            has_teaching_cloud_password=self.teaching_cloud_password is not None,
        )
