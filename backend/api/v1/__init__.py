"""API v1 routers — aggregate include happens in :mod:`backend.api.main`."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.api.deps import get_current_user
from backend.api.v1 import (
    accounts,
    chat,
    decisions,
    intents,
    messages,
    presets,
    products,
    rules,
    runs,
    skills,
    workspaces,
)
from backend.api.v1 import (
    settings as api_settings,
)

# Every v1 route requires a verified principal. The per-route workspace
# resolution (get_workspace_id) layers on top; this router-level dependency
# guarantees even routes without it (settings, presets list) return 401 when
# unauthenticated.
router = APIRouter(prefix="/v1", dependencies=[Depends(get_current_user)])
router.include_router(chat.router, prefix="/chat", tags=["chat"])
router.include_router(workspaces.router, prefix="/workspaces", tags=["workspaces"])
router.include_router(products.router, prefix="/products", tags=["products"])
router.include_router(accounts.router, prefix="/accounts", tags=["accounts"])
router.include_router(rules.router, prefix="/rules", tags=["rules"])
router.include_router(intents.router, prefix="/intents", tags=["intents"])
router.include_router(presets.router, prefix="/presets", tags=["presets"])
router.include_router(skills.router, prefix="/skills", tags=["skills"])
router.include_router(decisions.router, prefix="/decisions", tags=["decisions"])
router.include_router(api_settings.router, prefix="/settings", tags=["settings"])
router.include_router(runs.router, prefix="/runs", tags=["runs"])
router.include_router(messages.router, prefix="/messages", tags=["messages"])

__all__ = ["router"]
