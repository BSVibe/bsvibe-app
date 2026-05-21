"""ModelAccount — per-workspace, per-account LLM provider credentials.

Each :class:`ModelAccount` row carries the credentials + jurisdiction +
provider config the gateway needs to dispatch an LLM call. Multi-account
means routing rules / usage logs / budget policies are scoped to
``(workspace_id, account_id)``, not just ``workspace_id``.
"""

from __future__ import annotations

from backend.gateway.accounts.crypto import (
    CredentialCipher,
    decrypt_credentials,
    encrypt_credentials,
)
from backend.gateway.accounts.models import GatewayBase, ModelAccount
from backend.gateway.accounts.repository import ModelAccountRepository
from backend.gateway.accounts.schemas import (
    ModelAccountCreate,
    ModelAccountOut,
    ModelAccountUpdate,
)
from backend.gateway.accounts.service import (
    DEFAULT_ACCOUNT_LABEL,
    ModelAccountService,
)

__all__ = [
    "DEFAULT_ACCOUNT_LABEL",
    "CredentialCipher",
    "GatewayBase",
    "ModelAccount",
    "ModelAccountCreate",
    "ModelAccountOut",
    "ModelAccountRepository",
    "ModelAccountService",
    "ModelAccountUpdate",
    "decrypt_credentials",
    "encrypt_credentials",
]
