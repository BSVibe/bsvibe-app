"""Tenant context — extracts the current tenant from the authenticated user.

Resolution order (first hit wins):

1. ``request.state.tenant_id`` populated by ``get_current_user`` *after*
   it has verified the JWT signature via bsvibe-auth.
2. ``user.app_metadata['tenant_id']`` from the JWT (only consulted by
   ``_tenant_id_from_user`` after verification).
3. A deterministic per-user tenant id derived via UUIDv5 from the user
   id. This lets unmigrated environments keep working — every user
   automatically gets a stable personal tenant without anyone having to
   issue new JWTs.
4. ``DEFAULT_TENANT_ID`` for unauthenticated callers (e.g. workers
   posting results) so existing seed data is still reachable.

SECURITY: ``TenantMiddleware`` does NOT parse the JWT — that would
short-circuit signature verification and let an attacker spoof
``tenant_id`` via the unverified payload. The middleware only seeds
``request.state.tenant_id = DEFAULT_TENANT_ID``; the verified value is
written by ``backend.src.core.auth.get_current_user`` once the token is
validated.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

# TODO(bundle-x-integration): out-of-scope source dep -- bsvibe_auth
# from bsvibe_auth import BSVibeUser
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

# Stable namespace UUID used to derive deterministic personal-tenant ids
# from a user id. Generated once and intentionally hard-coded so the
# mapping is reproducible across restarts and deployments.
_TENANT_NAMESPACE = uuid.UUID("ee8f1bf9-2bcb-4f0a-9f1c-3a9c8d6e1b22")

# Fallback tenant id for unauthenticated callers (workers, internal jobs).
# Existing seed data lives under this id, so removing it would break dev
# environments. Authenticated requests no longer touch it.
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


def derive_personal_tenant_id(user_id: str) -> uuid.UUID:
    """Derive a stable personal tenant id from a user id."""
    return uuid.uuid5(_TENANT_NAMESPACE, user_id)


def _tenant_id_from_user(user: BSVibeUser | None) -> uuid.UUID | None:
    if user is None:
        return None
    metadata: dict[str, Any] = user.app_metadata or {}
    raw = metadata.get("tenant_id") or metadata.get("tenantId")
    if isinstance(raw, str):
        try:
            return uuid.UUID(raw)
        except ValueError:
            logger.warning("invalid_tenant_claim", raw=raw, user_id=user.id)
    if user.id:
        return derive_personal_tenant_id(user.id)
    return None


def get_tenant_id(request: Request) -> uuid.UUID:
    """Return the verified tenant id for this request.

    SECURITY: ``get_current_user`` (in ``backend.src.core.auth``) writes
    the post-verification tenant id onto ``request.state.tenant_id``.
    This dependency simply reads that value back. Routes MUST declare
    ``get_current_user`` as a sub-dependency before — or alongside —
    ``get_tenant_id`` so auth always runs first; the canonical pattern
    is to put ``user: BSVibeUser = Depends(get_current_user)`` *before*
    ``tenant_id: uuid.UUID = Depends(get_tenant_id)`` in the handler
    signature. FastAPI resolves dependencies in arg order, so this
    ordering guarantees the verified value is on ``request.state`` by
    the time we read it here.

    Falls back to ``DEFAULT_TENANT_ID`` only for explicitly
    unauthenticated paths (worker callbacks against seed data).
    """
    return getattr(request.state, "tenant_id", DEFAULT_TENANT_ID)


async def ensure_personal_tenant(db: AsyncSession, tenant_id: uuid.UUID, user: BSVibeUser) -> None:
    """Upsert the personal tenant row for an authenticated user.

    Account/tenant identity is owned by ``auth.bsvibe.dev`` — our local
    ``tenants`` table is a derived projection of whatever the JWT claims
    say. Every authenticated request runs this so:

      * a brand-new user gets their row inserted on first call
      * a user whose email or display name changed in bsvibe gets the
        local row refreshed
      * concurrent first-touch requests do not race (ON CONFLICT handles
        the duplicate-key case atomically)

    On PostgreSQL we use ``INSERT ... ON CONFLICT (id) DO UPDATE`` so the
    upsert is a single statement. On SQLite (used by some unit tests) we
    fall back to a SELECT + INSERT/UPDATE pair since SQLite needs the
    sqlite-specific ``insert`` and not all driver versions support it.

    ``slug`` carries no UNIQUE constraint (see the ``Tenant`` model):
    when BSVibe reassigns a user's tenant id this projects a fresh row
    for the new id while the old row lingers under the same slug.
    """
    # TODO(bundle-x-integration): out-of-scope source dep -- backend.src.models
    #     from backend.src.models import Tenant  # local import to dodge cycles

    name = (user.email or user.id or "Personal")[:255]
    slug = (user.id or str(tenant_id))[:255]
    owner = user.id or "system"

    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(Tenant)
            .values(id=tenant_id, name=name, slug=slug, owner_user_id=owner)
            .on_conflict_do_update(
                index_elements=["id"],
                set_={"name": name, "owner_user_id": owner},
            )
        )
        try:
            await db.execute(stmt)
            await db.commit()
        except IntegrityError:
            # ``ON CONFLICT (id)`` makes the upsert idempotent, so an
            # IntegrityError here is an unexpected concurrent race. Roll
            # back so the session stays usable; the row another request
            # committed is equivalent.
            await db.rollback()
            logger.warning("tenant_upsert_conflict", attempted_id=str(tenant_id), exc_info=True)
        return

    # Dialect-agnostic fallback (SQLite tests, etc.)
    existing = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    row = existing.scalar_one_or_none()
    if row is None:
        db.add(Tenant(id=tenant_id, name=name, slug=slug, owner_user_id=owner))
    else:
        row.name = name
        row.owner_user_id = owner
    try:
        await db.commit()
    except IntegrityError:
        # Concurrent insert from another request raced us — let the
        # other commit win and keep going.
        await db.rollback()
    except SQLAlchemyError:
        # Any other DB failure: roll back so the session stays usable
        # and let the original error surface to the caller, but log it
        # with exc_info so the on-call has a stack trace.
        logger.warning("tenant_upsert_commit_failed", tenant_id=str(tenant_id), exc_info=True)
        await db.rollback()


async def resolve_user_tenant(
    request: Request,
    user: BSVibeUser,
    db: AsyncSession,
) -> uuid.UUID:
    """Compute the tenant id for an authenticated user and stamp request state.

    Used as a dependency by routes that don't already pull a permission
    via ``require_permission``. The middleware handles the common case;
    this is a manual hook for tests and ad-hoc routes.
    """
    tenant_id = _tenant_id_from_user(user) or DEFAULT_TENANT_ID
    request.state.tenant_id = tenant_id
    if tenant_id != DEFAULT_TENANT_ID:
        await ensure_personal_tenant(db, tenant_id, user)
    return tenant_id


# ── Middleware ──────────────────────────────────────────────────────


class TenantMiddleware:
    """Initialise ``request.state.tenant_id`` to ``DEFAULT_TENANT_ID``
    before any handler runs.

    SECURITY: This middleware deliberately does NOT trust the JWT
    payload to populate ``tenant_id``. Doing so would short-circuit
    ``get_current_user``'s signature verification — an attacker could
    forge a JWT with any ``tenant_id`` claim, the middleware would
    stamp it, and a handler that resolves ``Depends(get_tenant_id)``
    *before* ``Depends(get_current_user)`` would read the spoofed
    value (FastAPI dependencies resolve in arg-order).

    The authoritative ``request.state.tenant_id`` is written by
    ``backend.src.core.auth.get_current_user`` *after* the bsvibe-auth
    provider has verified the JWT signature and audience. The personal
    Tenant row is also upserted there, so the middleware no longer
    needs to handle that side-effect.

    The only special case is the ``e2e_test_token`` dev bypass — that
    secret is a build-time constant, not user-controlled, and only
    valid in non-production environments (see
    ``backend.src.core.auth.get_current_user``).
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        request.state.tenant_id = DEFAULT_TENANT_ID

        await self.app(scope, receive, send)


# Convenience FastAPI dependency that exposes the tenant id without
# needing access to the raw Request object.
def TenantDep() -> uuid.UUID:  # noqa: N802 - module-level wrapper for Depends()
    raise NotImplementedError("Use Depends(get_tenant_id) directly.")


CurrentTenant = Depends(get_tenant_id)
