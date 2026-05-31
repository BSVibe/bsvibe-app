"""Workspace-scoped account entities.

Per Workflow §3, a workspace owns ``(n)`` :class:`ModelAccount` rows
(LLM provider credentials) alongside Membership / Product /
ConnectorAccount / Resource. The gateway consumes ``ModelAccount`` for
dispatch — but the entity itself lives at the workspace layer, not
inside any single role module.

(``ConnectorAccount`` will land alongside ``ModelAccount`` in a later
bundle when the plugin intake path is wired in.)
"""

from __future__ import annotations

from backend.router.accounts.crypto import (
    CredentialCipher,
    decrypt_credentials,
    encrypt_credentials,
)
from backend.router.accounts.models import AccountsBase, ModelAccount
from backend.router.accounts.repository import ModelAccountRepository
from backend.router.accounts.schemas import (
    ModelAccountCreate,
    ModelAccountOut,
    ModelAccountUpdate,
)
from backend.router.accounts.service import (
    DEFAULT_ACCOUNT_LABEL,
    ModelAccountService,
)

__all__ = [
    "DEFAULT_ACCOUNT_LABEL",
    "AccountsBase",
    "CredentialCipher",
    "ModelAccount",
    "ModelAccountCreate",
    "ModelAccountOut",
    "ModelAccountRepository",
    "ModelAccountService",
    "ModelAccountUpdate",
    "decrypt_credentials",
    "encrypt_credentials",
]
