"""Pydantic models exposed to package consumers.

`ServiceTokenPayload` matches the BSVibe-Auth PR #3 contract verbatim
(see ``BSVibe-Auth/phase0/auth-app/api/_lib/service-token.ts:51-62``).
Drift here would break service-to-service auth across all 4 products.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Post-Round-5 reversion: service-token audiences are again ``bs``-prefixed
# product names (bsage, bsgateway, bsupervisor, bsnexus). The bare-name MCP
# grammar from Round 5 was an intermediate step; unified back to ``bs*`` so
# audience and product identity match everywhere. (The ``bsvibe-auth``
# internal carve-out is handled in the auth-app, not here.)
ServiceAudience = Literal["bsage", "bsgateway", "bsupervisor", "bsnexus"]
SERVICE_AUDIENCES: frozenset[str] = frozenset(("bsage", "bsgateway", "bsupervisor", "bsnexus"))
TenantRole = Literal["owner", "admin", "member", "viewer"]
TenantPlan = Literal["free", "pro", "team", "enterprise"]
TenantType = Literal["personal", "org"]

PERMISSION_PATTERN = re.compile(r"^[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*$")
# Service-token scope grammar — MCP only: ``<audience>:<resource>`` with
# resource = ``*``, identifier, or dotted/dashed identifier
# (``gateway:*``, ``supervisor:audit.write``).
SCOPE_PATTERN = re.compile(r"^[a-z][a-z0-9-]*:(?:\*|[a-z][a-z0-9-]*(?:[._-][a-z0-9]+)*)$")


@dataclass(frozen=True, slots=True)
class Permission:
    """`<product>.<resource>.<action>` permission identifier."""

    product: str
    resource: str
    action: str

    def __str__(self) -> str:
        return f"{self.product}.{self.resource}.{self.action}"

    @classmethod
    def parse(cls, value: str) -> Permission:
        if not PERMISSION_PATTERN.match(value):
            raise ValueError(
                f"invalid permission identifier: {value!r} (expected '<product>.<resource>.<action>')",
            )
        product, resource, action = value.split(".")
        return cls(product=product, resource=resource, action=action)


class TenantMembership(BaseModel):
    """One row of the user's tenant memberships, denormalised onto User."""

    model_config = ConfigDict(extra="ignore")

    id: str
    role: TenantRole
    plan: TenantPlan = "free"
    type: TenantType = "personal"
    name: str | None = None


class User(BaseModel):
    """Authenticated principal — either a human or a service identity."""

    model_config = ConfigDict(extra="ignore")

    id: str
    email: str | None = None
    active_tenant_id: str | None = None
    tenants: list[TenantMembership] = Field(default_factory=list)
    is_service: bool = False
    # Demo-mode session: token is signed by demo_jwt_secret (not the
    # prod user_jwt_secret), permissions short-circuit to allow.
    is_demo: bool = False
    scope: list[str] = Field(default_factory=list)
    # Supabase-style claim payload extensions. ``parse_user_token`` lifts
    # them off the verified user JWT so consumers (BSGateway, BSNexus)
    # can read role / tenant_id / custom claims without a second decode.
    # Empty for bootstrap, opaque, and PAT-JWT-introspection flows.
    app_metadata: dict[str, Any] = Field(default_factory=dict)
    user_metadata: dict[str, Any] = Field(default_factory=dict)

    def role_in(self, tenant_id: str) -> TenantRole | None:
        for t in self.tenants:
            if t.id == tenant_id:
                return t.role
        return None

    def has_tenant(self, tenant_id: str) -> bool:
        return any(t.id == tenant_id for t in self.tenants)


class IntrospectionResponse(BaseModel):
    """RFC 7662 Token Introspection response.

    An inactive token MAY consist solely of ``{"active": false}`` per §2.2,
    so all other fields are optional.

    ``scope`` is normalized to a list. Per RFC 7662 §2.2 the wire format is a
    space-delimited string; some issuers send a list directly, so accept
    both shapes.
    """

    model_config = ConfigDict(extra="ignore")

    active: bool
    sub: str | None = None
    tenant: str | None = None
    aud: list[str] | None = None
    scope: list[str] | None = None
    exp: int | None = None
    client_id: str | None = None
    username: str | None = None
    # Tier 5: the caller's tenant role (owner/admin/member/viewer) for the
    # introspected token's tenant. Lets verify_via_introspection populate
    # User.app_metadata so PAT requests drive require_permission's lazy
    # tuple-provisioning + require_admin — both otherwise dead for PATs.
    role: str | None = None

    @field_validator("scope", mode="before")
    @classmethod
    def _split_scope_string(cls, value: object) -> object:
        if isinstance(value, str):
            return value.split() if value else []
        return value


class ServiceTokenPayload(BaseModel):
    """Decoded service JWT payload.

    Mirrors `ServiceTokenPayload` from BSVibe-Auth PR #3:
    iss, sub, aud (one of bsage/bsgateway/bsupervisor/bsnexus),
    scope (space-delimited string), iat, exp, token_type="service",
    optional tenant_id.
    """

    model_config = ConfigDict(extra="forbid")

    iss: str
    sub: str
    aud: ServiceAudience
    scope: str
    iat: int
    exp: int
    token_type: Literal["service"]
    tenant_id: str | None = None

    @field_validator("scope")
    @classmethod
    def _validate_scope_format(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("scope must not be empty")
        for s in value.split():
            if not SCOPE_PATTERN.match(s):
                raise ValueError(f"invalid scope identifier: {s!r}")
        return value

    @property
    def scopes(self) -> list[str]:
        return self.scope.split()

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes
