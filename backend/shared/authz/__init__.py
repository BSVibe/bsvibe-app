"""Authentication for BSVibe — verify the caller's Supabase session JWT.

BSVibe is a single backend, so authorization reduces to three orthogonal
axes (Workflow §3):

- **Supabase JWT (ES256/JWKS)** = authentication → this package.
- **``Membership.role``** = authorization (RBAC) → :mod:`backend.identity.roles`
  + :func:`backend.api.deps.require_role`.
- **``workspace_id`` scoping** = isolation → :mod:`backend.data.scoping`.

OpenFGA (cross-service ReBAC), the service-token machinery, and RFC 7662
introspection were retired — they fit none of the three axes.

Public API:
- ``CurrentUser`` / ``get_current_user`` — authenticated :class:`User` from JWT
- ``Settings`` / ``get_settings`` — JWT verification configuration
- ``User`` — the authenticated principal
- ``verify_user_jwt`` / ``parse_user_token`` / ``AuthError`` / ``reset_jwks_cache``
"""

from __future__ import annotations

from .auth import (
    AuthError,
    parse_user_token,
    reset_jwks_cache,
    verify_user_jwt,
)
from .deps import (
    CurrentUser,
    get_current_user,
    get_settings_dep,
)
from .settings import Settings, get_settings, reset_settings_cache
from .types import User

__version__ = "3.0.0"

__all__ = [
    "AuthError",
    "CurrentUser",
    "Settings",
    "User",
    "__version__",
    "get_current_user",
    "get_settings",
    "get_settings_dep",
    "parse_user_token",
    "reset_jwks_cache",
    "reset_settings_cache",
    "verify_user_jwt",
]
