"""API v1 routers — aggregate include happens in :mod:`backend.api.main`."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.api.deps import get_current_user
from backend.api.v1 import (
    account,
    accounts,
    chat,
    checkpoints,
    connector_oauth,
    connectors,
    decisions,
    deliverables,
    inside,
    intents,
    messages,
    notifications,
    presets,
    products,
    rules,
    run_routing,
    runs,
    safemode,
    skills,
    workers,
    workspace,
    workspace_compliance,
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
# GDPR L1 — Art. 15 / 20 export + Art. 30 processing record. Singular
# /workspace because both routes operate on the *caller's* one resolved
# workspace, not the plural /workspaces membership lookup above.
router.include_router(
    workspace_compliance.router, prefix="/workspace", tags=["workspace-compliance"]
)
# Everyday workspace metadata (GET / PATCH name). Same /workspace prefix —
# the two routers' paths don't overlap (/export, /processing-record vs the
# root /). FastAPI merges them under one prefix without conflict.
router.include_router(workspace.router, prefix="/workspace", tags=["workspace"])
router.include_router(products.router, prefix="/products", tags=["products"])
router.include_router(accounts.router, prefix="/accounts", tags=["accounts"])
# Singular /account — personal billing-account discovery (distinct from the
# plural /accounts ModelAccount CRUD above).
router.include_router(account.router, prefix="/account", tags=["account"])
router.include_router(connectors.router, prefix="/connectors", tags=["connectors"])
# OAuth connect (founder-authed /start only; the public /callback is mounted
# outside this auth-gated router in backend.api.main).
router.include_router(
    connector_oauth.router, prefix="/connectors/oauth", tags=["connectors"]
)
router.include_router(rules.router, prefix="/rules", tags=["rules"])
router.include_router(run_routing.router, prefix="/run-routing", tags=["run-routing"])
router.include_router(intents.router, prefix="/intents", tags=["intents"])
router.include_router(presets.router, prefix="/presets", tags=["presets"])
router.include_router(skills.router, prefix="/skills", tags=["skills"])
router.include_router(decisions.router, prefix="/decisions", tags=["decisions"])
router.include_router(checkpoints.router, prefix="/checkpoints", tags=["checkpoints"])
router.include_router(api_settings.router, prefix="/settings", tags=["settings"])
router.include_router(runs.router, prefix="/runs", tags=["runs"])
router.include_router(deliverables.router, prefix="/deliverables", tags=["deliverables"])
router.include_router(messages.router, prefix="/messages", tags=["messages"])
router.include_router(safemode.router, prefix="/safemode", tags=["safemode"])
router.include_router(inside.router, prefix="/inside", tags=["inside"])
router.include_router(notifications.router, prefix="/notifications", tags=["notifications"])
# Workers — only the JWT-gated routes (install-token mint, list, revoke) live
# under the v1 aggregate. The install-token/worker-token authed routes
# (register, heartbeat) are mounted by backend.api.main on ``workers.public_router``
# at /api/v1/workers directly, bypassing the get_current_user gate.
router.include_router(workers.router, prefix="/workers", tags=["workers"])

# Embedded OAuth client management (Lift D1, RFC 7591). The DCR public
# endpoints (authorize/token/introspect/revoke) live on
# ``backend.api.oauth.public_router`` which :mod:`backend.api.main` mounts
# at /api/oauth — outside this auth-gated aggregate.
from backend.api.oauth import v1_router as _oauth_v1_router  # noqa: E402, PLC0415

router.include_router(_oauth_v1_router)

__all__ = ["router"]
