"""API v1 routers — aggregate include happens in :mod:`backend.api.main`."""

from __future__ import annotations

from fastapi import APIRouter

from backend.api.v1 import (
    accounts,
    chat,
    decisions,
    intents,
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

router = APIRouter(prefix="/v1")
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

__all__ = ["router"]
