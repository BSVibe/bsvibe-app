"""bsvibe-authz — shared authorization library for BSVibe Python services.

Public API surface (Phase 0 P0.4):

- ``CurrentUser`` / ``get_current_user`` — extract authenticated User from JWT
- ``require_permission`` — FastAPI dep factory that calls OpenFGA
- ``ServiceKeyAuth`` / ``ServiceKey`` — service-to-service JWT verification
- ``get_active_tenant_id`` — TenantScoped helper
- ``OpenFGAClient`` — async wrapper for OpenFGA HTTP API
- ``PermissionCache`` — 30s TTL decision cache
- ``Settings`` / ``get_settings`` — pydantic-settings configuration
- ``User`` / ``Permission`` / ``ServiceTokenPayload`` / ``TenantMembership``
- ``verify_user_jwt`` / ``verify_service_jwt`` / ``parse_user_token`` / ``AuthError``

Lock-in references
------------------
- Decision #15 (BaseServiceClient = Protocol): see ``deps.FGAClientProtocol``
- Decision #16 (service JWT audience-scoped + scope claim): see
  ``auth.verify_service_jwt`` and ``types.ServiceTokenPayload``
"""

from __future__ import annotations

from .auth import (
    AuthError,
    parse_user_token,
    verify_service_jwt,
    verify_user_jwt,
    verify_via_introspection,
)
from .cache import IntrospectionCache, PermissionCache
from .client import OpenFGAAuthError, OpenFGAClient, OpenFGAError
from .deps import (
    CurrentUser,
    FGAClientProtocol,
    ServiceKey,
    ServiceKeyAuth,
    check_permission,
    check_tenant_permission,
    combined_principal,
    get_active_tenant_id,
    get_current_user,
    get_introspection_cache,
    get_introspection_client,
    get_openfga_client,
    get_permission_cache,
    get_settings_dep,
    require_admin,
    require_permission,
    reset_singletons,
)
from .introspection import IntrospectionClient
from .service_token_minter import ServiceTokenMinter, ServiceTokenMinterError
from .settings import Settings, get_settings, reset_settings_cache
from .types import (
    SERVICE_AUDIENCES,
    IntrospectionResponse,
    Permission,
    ServiceAudience,
    ServiceTokenPayload,
    TenantMembership,
    TenantPlan,
    TenantRole,
    TenantType,
    User,
)

__version__ = "2.1.1"

__all__ = [
    "AuthError",
    "CurrentUser",
    "FGAClientProtocol",
    "IntrospectionCache",
    "IntrospectionClient",
    "IntrospectionResponse",
    "OpenFGAAuthError",
    "OpenFGAClient",
    "OpenFGAError",
    "Permission",
    "PermissionCache",
    "SERVICE_AUDIENCES",
    "ServiceAudience",
    "ServiceKey",
    "ServiceKeyAuth",
    "ServiceTokenMinter",
    "ServiceTokenMinterError",
    "ServiceTokenPayload",
    "Settings",
    "TenantMembership",
    "TenantPlan",
    "TenantRole",
    "TenantType",
    "User",
    "__version__",
    "check_permission",
    "check_tenant_permission",
    "combined_principal",
    "get_active_tenant_id",
    "get_current_user",
    "get_introspection_cache",
    "get_introspection_client",
    "get_openfga_client",
    "get_permission_cache",
    "get_settings",
    "get_settings_dep",
    "parse_user_token",
    "require_admin",
    "require_permission",
    "reset_settings_cache",
    "reset_singletons",
    "verify_service_jwt",
    "verify_user_jwt",
    "verify_via_introspection",
]
