"""The credential a ship hands to publish, for a product cloned shallow (#929).

A ``--depth=1`` clone of a PRIVATE repository cannot be repaired anonymously, and the clone
token is never kept on disk. The ship path holds the run's workspace session, so it resolves
the workspace's github credential there — lazily: publish asks only when the repo is still
shallow, and a refresh writes the rotated token back in the ship's own transaction.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from backend.storage.product_workspace import Credential

logger = structlog.get_logger(__name__)


async def _resolve_github_token(session: Any, *, workspace_id: uuid.UUID, cipher: Any) -> Any:
    from backend.connectors.auth.resolve import resolve_github_token  # noqa: PLC0415

    return await resolve_github_token(session, workspace_id=workspace_id, cipher=cipher)


def _cipher() -> Any:
    from backend.router.accounts.crypto import cipher_from_settings  # noqa: PLC0415

    return cipher_from_settings()


def product_repo_credential(session: Any, workspace_id: uuid.UUID) -> Credential:
    """A callback publish may call for the product repo's git credential (``None`` if none)."""

    async def _credential() -> str | None:
        try:
            token = await _resolve_github_token(
                session, workspace_id=workspace_id, cipher=_cipher()
            )
        except Exception:  # noqa: BLE001 — no credential is a refused publish, not a failed ship
            logger.warning(
                "product_repo_credential_unavailable",
                workspace_id=str(workspace_id),
                exc_info=True,
            )
            return None
        return str(token) if token else None

    return _credential


__all__ = ["product_repo_credential"]
