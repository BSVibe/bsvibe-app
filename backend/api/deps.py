"""FastAPI dependencies for v1 routes.

Workspace + account scoping is extracted from the verified Supabase JWT
(raw ES256, post-Tier 3.2 — no wrapped JWT). Most routes call
``Depends(get_workspace_id)`` so the dependency tree fails fast when
either auth or workspace membership is missing.

All concrete auth resolution lands in Bundle G integration; for now the
dependencies return placeholders so the route surface can be wired and
tests can run against a mocked dependency override.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status


async def get_current_user() -> dict:
    """Stub — Bundle G replaces with backend.shared.authz.deps."""
    # TODO(bundle-api-integration): wire via backend.shared.authz.deps.dispatch_pat_jwt
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="auth dependency not wired (Bundle API skeleton)",
    )


async def get_workspace_id(
    user: Annotated[dict, Depends(get_current_user)],
) -> uuid.UUID:
    """Pull workspace_id from JWT app_metadata."""
    ws = user.get("workspace_id")
    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="JWT missing workspace_id",
        )
    return uuid.UUID(str(ws))


async def get_account_id(
    user: Annotated[dict, Depends(get_current_user)],
) -> uuid.UUID | None:
    """Pull optional account_id from JWT or request metadata."""
    return uuid.UUID(str(user["account_id"])) if "account_id" in user else None
