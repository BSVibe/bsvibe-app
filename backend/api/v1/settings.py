"""settings CRUD — Bundle API skeleton."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

router = APIRouter()


@router.get("")
async def list_settings() -> list[dict]:
    """List settings for the current workspace."""
    # TODO(bundle-api-integration): wire to concrete service layer.
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="settings not yet wired (Bundle API skeleton)",
    )
