"""types.py — pydantic models."""

from __future__ import annotations

import pytest


def test_user_model_is_pydantic_with_role_and_tenants() -> None:
    from backend.shared.authz.types import TenantMembership, User

    u = User(
        id="00000000-0000-0000-0000-000000000001",
        email="alice@bsvibe.dev",
        active_tenant_id="t-1",
        tenants=[
            TenantMembership(id="t-1", role="owner", plan="pro", type="personal"),
            TenantMembership(id="t-2", role="member", plan="team", type="org"),
        ],
    )
    assert u.is_service is False
    assert u.role_in("t-1") == "owner"
    assert u.role_in("t-9") is None
    assert u.has_tenant("t-2") is True


def test_service_user_marker() -> None:
    from backend.shared.authz.types import User

    u = User(
        id="service:bsnexus",
        email=None,
        active_tenant_id="t-1",
        tenants=[],
        is_service=True,
    )
    assert u.is_service is True


def test_service_token_payload_matches_bsvibe_auth_pr3_contract() -> None:
    """Regression — payload shape must match BSVibe-Auth PR #3.

    Reference fixture (TS):
      iss, sub, aud (one of bsage/bsgateway/bsupervisor/bsnexus),
      scope (space-delimited string), iat, exp, token_type="service",
      tenant_id (optional)
    """
    from backend.shared.authz.types import ServiceTokenPayload

    payload = ServiceTokenPayload(
        iss="https://auth.bsvibe.dev",
        sub="service:bsnexus",
        aud="bsage",
        scope="bsage:read bsage:write",
        iat=1733823600,
        exp=1733827200,
        token_type="service",
        tenant_id="t-1",
    )
    assert payload.aud == "bsage"
    assert payload.scopes == ["bsage:read", "bsage:write"]
    assert payload.has_scope("bsage:read") is True
    assert payload.has_scope("bsgateway:read") is False


def test_service_token_payload_rejects_invalid_audience() -> None:
    from backend.shared.authz.types import ServiceTokenPayload

    with pytest.raises(ValueError):
        ServiceTokenPayload(
            iss="https://auth.bsvibe.dev",
            sub="service:bsnexus",
            aud="invalid-aud",  # type: ignore[arg-type]
            scope="bsage:read",
            iat=1,
            exp=2,
            token_type="service",
        )


def test_service_token_payload_rejects_wrong_token_type() -> None:
    from backend.shared.authz.types import ServiceTokenPayload

    with pytest.raises(ValueError):
        ServiceTokenPayload(
            iss="https://auth.bsvibe.dev",
            sub="service:x",
            aud="bsage",
            scope="bsage:read",
            iat=1,
            exp=2,
            token_type="user",  # type: ignore[arg-type]
        )


def test_permission_string_validation() -> None:
    from backend.shared.authz.types import Permission

    p = Permission.parse("bsnexus.project.read")
    assert p.product == "bsnexus"
    assert p.resource == "project"
    assert p.action == "read"
    assert str(p) == "bsnexus.project.read"

    with pytest.raises(ValueError):
        Permission.parse("invalidformat")


def test_user_has_scope_field_default_empty() -> None:
    from backend.shared.authz.types import User

    u = User(id="alice")
    assert u.scope == []


def test_user_accepts_scope_list() -> None:
    from backend.shared.authz.types import User

    u = User(id="bootstrap", scope=["*"])
    assert u.scope == ["*"]


def test_introspection_response_inactive_short_form() -> None:
    """RFC 7662 §2.2 — an inactive token MAY omit all other fields."""
    from backend.shared.authz.types import IntrospectionResponse

    r = IntrospectionResponse.model_validate({"active": False})
    assert r.active is False
    assert r.sub is None
    assert r.tenant is None
    assert r.aud is None
    assert r.scope is None
    assert r.exp is None
    assert r.client_id is None
    assert r.username is None


def test_introspection_response_full_active_form() -> None:
    from backend.shared.authz.types import IntrospectionResponse

    r = IntrospectionResponse.model_validate(
        {
            "active": True,
            "sub": "user-123",
            "tenant": "t-1",
            "aud": ["bsage", "bsgateway"],
            "scope": ["bsgateway:models:read", "bsgateway:models:write"],
            "exp": 1733827200,
            "client_id": "client-abc",
            "username": "alice@bsvibe.dev",
        }
    )
    assert r.active is True
    assert r.sub == "user-123"
    assert r.tenant == "t-1"
    assert r.aud == ["bsage", "bsgateway"]
    assert r.scope == ["bsgateway:models:read", "bsgateway:models:write"]
    assert r.exp == 1733827200
    assert r.client_id == "client-abc"
    assert r.username == "alice@bsvibe.dev"


def test_introspection_response_ignores_unknown_fields() -> None:
    from backend.shared.authz.types import IntrospectionResponse

    r = IntrospectionResponse.model_validate({"active": True, "unknown_extension": "x"})
    assert r.active is True


def test_introspection_response_accepts_string_scope() -> None:
    """RFC 7662 §2.2 — scope is a space-delimited string."""
    from backend.shared.authz.types import IntrospectionResponse

    r = IntrospectionResponse.model_validate(
        {"active": True, "scope": "bsgateway:models:read bsage:notes:read"}
    )
    assert r.scope == ["bsgateway:models:read", "bsage:notes:read"]


def test_introspection_response_accepts_star_scope_string() -> None:
    """Bootstrap-style ``*`` scope from BSVibe-Auth introspect endpoint."""
    from backend.shared.authz.types import IntrospectionResponse

    r = IntrospectionResponse.model_validate({"active": True, "scope": "*"})
    assert r.scope == ["*"]


def test_introspection_response_accepts_empty_string_scope() -> None:
    from backend.shared.authz.types import IntrospectionResponse

    r = IntrospectionResponse.model_validate({"active": True, "scope": ""})
    assert r.scope == []
