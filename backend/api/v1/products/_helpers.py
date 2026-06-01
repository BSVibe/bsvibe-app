"""Shared adapter helpers for the ``/api/v1/products`` surface (Lift M1).

The two ``_resolve_*_in_workspace`` helpers enforce the structural workspace
boundary every endpoint mutation depends on — a row that doesn't belong to the
caller's workspace 404s uniformly here, not deeper in the repo. Pulled into
one shared module so resources / bindings / files endpoints stay D35-thin.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.db import ConnectorAccountRow
from backend.identity.workspaces_db import ProductRow

# Read cap for product file content (mirrors the deliverable artifact viewer):
# a source file is small; this guards against a large blob slipping into a JSON
# body. Past the cap the leading bytes are returned with ``truncated: true``.
_MAX_FILE_BYTES = 256 * 1024


def _looks_binary(raw: bytes) -> bool:
    """A NUL byte in the first 8 KiB ⇒ treat as binary (don't stream raw bytes
    into a JSON text field)."""
    return b"\x00" in raw[:8192]


async def _resolve_product_in_workspace(
    session: AsyncSession, product_id: uuid.UUID, workspace_id: uuid.UUID
) -> ProductRow:
    """Load a product the caller's workspace owns, or raise 404."""
    product = await session.get(ProductRow, product_id)
    if product is None or product.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Product {product_id} not found"
        )
    return product


async def _resolve_connector_account_in_workspace(
    session: AsyncSession,
    connector_account_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> ConnectorAccountRow:
    """Load a connector account the caller's workspace owns, or 404.

    Mirror of :func:`_resolve_product_in_workspace`. Returning 404 (not 400)
    keeps the surface uniform with "this thing isn't here for you".
    """
    account = await session.get(ConnectorAccountRow, connector_account_id)
    if account is None or account.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"connector_account {connector_account_id} not found",
        )
    return account


__all__ = [
    "_MAX_FILE_BYTES",
    "_looks_binary",
    "_resolve_connector_account_in_workspace",
    "_resolve_product_in_workspace",
]
