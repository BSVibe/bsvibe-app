"""FastAPI Depends integration tests."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from backend.shared.authz import deps as deps_mod
from backend.shared.authz.cache import IntrospectionCache
from backend.shared.authz.deps import (
    CurrentUser,
    ServiceKey,
    ServiceKeyAuth,
    require_permission,
)
from backend.shared.authz.settings import Settings
from backend.shared.authz.types import IntrospectionResponse, User


@pytest.fixture
def deps_settings(user_jwt_secret: str, service_signing_secret: str, issuer: str):
    return Settings(  # type: ignore[call-arg]
        bsvibe_auth_url=issuer,
        openfga_api_url="http://openfga.local:8080",
        openfga_store_id="store-1",
        openfga_auth_model_id="model-1",
        service_token_signing_secret=service_signing_secret,
        user_jwt_secret=user_jwt_secret,
        user_jwt_algorithm="HS256",
        user_jwt_audience="bsvibe",
        user_jwt_issuer=issuer,
    )


def _build_app(
    settings: Settings,
    *,
    fga_check: Callable[..., bool] | None = None,
    fga_write_raises: BaseException | None = None,
) -> FastAPI:
    # Reset process-wide singletons (cache, OpenFGA client) so tests are
    # hermetic — otherwise a `True` decision cached by an earlier test bleeds
    # into a later "denied" test.
    deps_mod.reset_singletons()
    app = FastAPI()

    writes: list[tuple[str, str, str]] = []
    checks: list[tuple[str, str, str]] = []

    class FakeFGA:
        async def check(self, user: str, relation: str, object_: str, **_: Any) -> bool:
            checks.append((user, relation, object_))
            if fga_check is None:
                return True
            return fga_check(user, relation, object_)

        async def list_objects(self, *args: Any, **kwargs: Any) -> list[str]:
            return []

        async def write_tuple(self, user: str, relation: str, object_: str) -> None:
            writes.append((user, relation, object_))
            if fga_write_raises is not None:
                raise fga_write_raises

    fake_fga = FakeFGA()
    # Expose the writes/checks lists so authz tests can inspect them.
    app.state.fga_writes = writes  # type: ignore[attr-defined]
    app.state.fga_checks = checks  # type: ignore[attr-defined]

    app.dependency_overrides[deps_mod.get_settings_dep] = lambda: settings
    app.dependency_overrides[deps_mod.get_openfga_client] = lambda: fake_fga

    @app.get("/me")
    async def me(user: CurrentUser) -> dict:
        return {"id": user.id, "email": user.email, "active_tenant_id": user.active_tenant_id}

    @app.get("/projects/{project_id}")
    async def project(
        project_id: str,
        user: CurrentUser,
        _allowed: None = Depends(
            require_permission(
                "bsnexus.project.read",
                resource_type="project",
                resource_id_param="project_id",
            ),
        ),
    ) -> dict:
        return {"id": project_id, "user": user.id}

    @app.get("/internal/data")
    async def internal_data(
        svc: ServiceKey = Depends(ServiceKeyAuth(audience="bsage")),
    ) -> dict:
        return {"sub": svc.sub, "scope": svc.scope}

    @app.get("/routing")
    async def routing(
        user: CurrentUser,
        _allowed: None = Depends(require_permission("bsgateway.routing.read")),
    ) -> dict:
        return {"user": user.id}

    return app


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_current_user_returns_user_from_valid_jwt(deps_settings, make_user_jwt) -> None:
    app = _build_app(deps_settings)
    with TestClient(app) as client:
        token = make_user_jwt(sub="u-1", email="alice@bsvibe.dev", active_tenant_id="t-1")
        resp = client.get("/me", headers=_bearer(token))
        assert resp.status_code == 200
        assert resp.json() == {
            "id": "u-1",
            "email": "alice@bsvibe.dev",
            "active_tenant_id": "t-1",
        }


def test_current_user_401_without_authorization(deps_settings) -> None:
    app = _build_app(deps_settings)
    with TestClient(app) as client:
        resp = client.get("/me")
        assert resp.status_code == 401


def test_current_user_401_invalid_token(deps_settings) -> None:
    app = _build_app(deps_settings)
    with TestClient(app) as client:
        resp = client.get("/me", headers=_bearer("not-a-jwt"))
        assert resp.status_code == 401


def test_require_permission_allows_when_fga_allows(deps_settings, make_user_jwt) -> None:
    app = _build_app(deps_settings, fga_check=lambda u, r, o: True)
    with TestClient(app) as client:
        token = make_user_jwt(sub="u-1")
        resp = client.get("/projects/p1", headers=_bearer(token))
        assert resp.status_code == 200


def test_require_permission_lazy_writes_tenant_tuple_from_app_metadata_role(
    deps_settings, make_user_jwt
) -> None:
    """Lazy auto-provision: when the caller's JWT carries ``app_metadata.role`` +
    ``active_tenant_id``, ``require_permission`` writes the
    ``user:X <role> tenant:T`` tuple before checking. This bridges the missing
    platform tenant-provisioning gap — without it every require_permission
    route 403s the moment OpenFGA is enabled."""
    app = _build_app(deps_settings, fga_check=lambda u, r, o: True)
    with TestClient(app) as client:
        token = make_user_jwt(
            sub="u-1",
            active_tenant_id="t-9",
            extra_claims={"app_metadata": {"role": "owner", "tenant_id": "t-9"}},
        )
        resp = client.get("/projects/p1", headers=_bearer(token))
        assert resp.status_code == 200
    assert ("user:u-1", "owner", "tenant:t-9") in app.state.fga_writes  # type: ignore[attr-defined]


def test_require_permission_lazy_write_swallows_openfga_error(deps_settings, make_user_jwt) -> None:
    """OpenFGA returns HTTP 400 for duplicate-write — that's the steady-state
    path. ``require_permission`` swallows ``OpenFGAError`` and continues to
    the check; a healthy tuple-already-exists must not 500 the request."""
    from backend.shared.authz.client import OpenFGAError

    app = _build_app(
        deps_settings,
        fga_check=lambda u, r, o: True,
        fga_write_raises=OpenFGAError(400, {"error": "tuple_exists"}),
    )
    with TestClient(app) as client:
        token = make_user_jwt(
            sub="u-1",
            active_tenant_id="t-9",
            extra_claims={"app_metadata": {"role": "admin", "tenant_id": "t-9"}},
        )
        resp = client.get("/projects/p1", headers=_bearer(token))
        assert resp.status_code == 200


def test_require_permission_no_lazy_write_without_role_claim(deps_settings, make_user_jwt) -> None:
    """Users without an ``app_metadata.role`` claim skip the tuple write —
    nothing to provision. The check still runs normally."""
    app = _build_app(deps_settings, fga_check=lambda u, r, o: True)
    with TestClient(app) as client:
        token = make_user_jwt(sub="u-1", active_tenant_id="t-9")
        resp = client.get("/projects/p1", headers=_bearer(token))
        assert resp.status_code == 200
    assert app.state.fga_writes == []  # type: ignore[attr-defined]


def test_require_permission_403_when_fga_denies(deps_settings, make_user_jwt) -> None:
    app = _build_app(deps_settings, fga_check=lambda u, r, o: False)
    with TestClient(app) as client:
        token = make_user_jwt(sub="u-1")
        resp = client.get("/projects/p1", headers=_bearer(token))
        assert resp.status_code == 403


def test_require_permission_tenant_scoped_relation_is_product_resource_action(
    deps_settings, make_user_jwt
) -> None:
    """Tier 5: a tenant-scoped require_permission check uses the full
    ``<product>_<resource>_<action>`` relation (not the bare action), so each
    resource×action is a distinct OpenFGA relation on the tenant type."""
    app = _build_app(deps_settings, fga_check=lambda u, r, o: True)
    with TestClient(app) as client:
        token = make_user_jwt(sub="u-1", active_tenant_id="t-1")
        resp = client.get("/routing", headers=_bearer(token))
        assert resp.status_code == 200
    assert ("user:u-1", "bsgateway_routing_read", "tenant:t-1") in app.state.fga_checks  # type: ignore[attr-defined]


def test_require_permission_resource_scoped_relation_stays_bare_action(
    deps_settings, make_user_jwt
) -> None:
    """An instance-scoped check (resource_type + resource_id_param) keeps the
    bare ``<action>`` relation against ``<resource_type>:<id>`` — the
    resource-instance types in the model define plain read/write/delete."""
    app = _build_app(deps_settings, fga_check=lambda u, r, o: True)
    with TestClient(app) as client:
        token = make_user_jwt(sub="u-1", active_tenant_id="t-1")
        resp = client.get("/projects/p1", headers=_bearer(token))
        assert resp.status_code == 200
    assert ("user:u-1", "read", "project:p1") in app.state.fga_checks  # type: ignore[attr-defined]


def test_service_key_auth_accepts_valid_service_token(deps_settings, make_service_jwt) -> None:
    app = _build_app(deps_settings)
    with TestClient(app) as client:
        token = make_service_jwt(
            sub="service:bsnexus",
            aud="bsage",
            scope="bsage:read bsage:write",
            tenant_id="t-1",
        )
        resp = client.get("/internal/data", headers=_bearer(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["sub"] == "service:bsnexus"
        assert body["scope"] == "bsage:read bsage:write"


def test_service_key_auth_rejects_user_jwt(deps_settings, make_user_jwt) -> None:
    app = _build_app(deps_settings)
    with TestClient(app) as client:
        token = make_user_jwt()
        resp = client.get("/internal/data", headers=_bearer(token))
        assert resp.status_code == 401


def test_service_key_auth_rejects_wrong_audience(deps_settings, make_service_jwt) -> None:
    app = _build_app(deps_settings)
    with TestClient(app) as client:
        token = make_service_jwt(aud="bsgateway")
        resp = client.get("/internal/data", headers=_bearer(token))
        assert resp.status_code == 401


def test_tenant_scoped_extracts_active_tenant(deps_settings, make_user_jwt) -> None:
    """TenantScoped dep mixin auto-injects active tenant id."""
    from backend.shared.authz import deps as deps_mod

    app = _build_app(deps_settings)

    @app.get("/tenant-id")
    async def tenant_route(
        tenant_id: str = Depends(deps_mod.get_active_tenant_id),
    ) -> dict:
        return {"tenant_id": tenant_id}

    with TestClient(app) as client:
        token = make_user_jwt(active_tenant_id="t-99")
        resp = client.get("/tenant-id", headers=_bearer(token))
        assert resp.status_code == 200
        assert resp.json() == {"tenant_id": "t-99"}


def test_tenant_scoped_403_when_no_active_tenant(deps_settings, user_jwt_secret, issuer) -> None:
    import jwt

    from backend.shared.authz import deps as deps_mod

    app = _build_app(deps_settings)

    @app.get("/tenant-id")
    async def tenant_route(
        tenant_id: str = Depends(deps_mod.get_active_tenant_id),
    ) -> dict:
        return {"tenant_id": tenant_id}

    import time

    now = int(time.time())
    token = jwt.encode(
        {
            "iss": issuer,
            "sub": "u-1",
            "email": "x@y.z",
            "aud": "bsvibe",
            "iat": now,
            "exp": now + 60,
        },
        user_jwt_secret,
        algorithm="HS256",
    )
    with TestClient(app) as client:
        resp = client.get("/tenant-id", headers=_bearer(token))
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# TASK-006: token dispatch
# ---------------------------------------------------------------------------
@pytest.fixture
def opaque_settings(deps_settings: Settings) -> Settings:
    return deps_settings.model_copy(
        update={
            "introspection_url": "https://auth.bsvibe.dev/oauth/introspect",
            "introspection_client_id": "bsage",
            "introspection_client_secret": "shh",
        },
    )


class _FakeIntrospectionClient:
    def __init__(self, response: IntrospectionResponse) -> None:
        self._response = response
        self.calls: list[str] = []

    async def introspect(self, token: str) -> IntrospectionResponse:
        self.calls.append(token)
        return self._response


def _build_dispatch_app(settings: Settings, fake_client: Any | None = None) -> FastAPI:
    deps_mod.reset_singletons()
    app = FastAPI()
    app.dependency_overrides[deps_mod.get_settings_dep] = lambda: settings
    if fake_client is not None:
        app.dependency_overrides[deps_mod.get_introspection_client] = lambda: fake_client
        app.dependency_overrides[deps_mod.get_introspection_cache] = lambda: IntrospectionCache(
            ttl_s=60
        )

    @app.get("/me")
    async def me(user: CurrentUser) -> dict:
        return {"id": user.id, "scope": user.scope, "is_service": user.is_service}

    return app


def test_dispatch_legacy_opaque_prefix_now_401(opaque_settings) -> None:
    """Tier-2 retirement: the legacy ``bsv_sk_*`` opaque-token prefix
    dispatch was removed. A ``bsv_sk_*`` token is now indistinguishable
    from any non-JWT garbage — it skips introspection (failing
    `_looks_like_jwt`) and 401s. The introspection fallback only runs for
    JWT-shaped tokens (PAT JWTs from the device-authorization grant)."""
    fake = _FakeIntrospectionClient(
        IntrospectionResponse(
            active=True,
            sub="user-123",
            tenant="t-1",
            scope=["bsgateway:models:read"],
        ),
    )
    app = _build_dispatch_app(opaque_settings, fake_client=fake)
    with TestClient(app) as client:
        resp = client.get("/me", headers=_bearer("bsv_sk_live_token"))
        assert resp.status_code == 401
    # Confirm introspection was never called — the legacy prefix-dispatch
    # is gone and ``bsv_sk_*`` no longer triggers a network round-trip.
    assert fake.calls == []


def test_dispatch_jwt_token(deps_settings, make_user_jwt) -> None:
    app = _build_dispatch_app(deps_settings)
    with TestClient(app) as client:
        token = make_user_jwt(sub="u-1")
        resp = client.get("/me", headers=_bearer(token))
        assert resp.status_code == 200
        assert resp.json()["id"] == "u-1"


# ---- PAT JWT introspection fallback ----------------------------------------
#
# Device-grant PATs are signed JWTs (HS256 + SERVICE_TOKEN_SIGNING_SECRET,
# not USER_JWT_SECRET) so they fail `verify_user_jwt`. The introspection
# endpoint accepts them by jti — the dispatcher falls back through
# introspection when user_jwt verification fails.


def test_dispatch_pat_jwt_falls_back_to_introspection(opaque_settings) -> None:
    """JWT not signed with user_jwt_secret → introspection picks it up."""
    fake = _FakeIntrospectionClient(
        IntrospectionResponse(
            active=True,
            sub="user-pat",
            tenant="t-1",
            scope=["bsgateway:models:read"],
        ),
    )
    app = _build_dispatch_app(opaque_settings, fake_client=fake)
    # JWT-shaped token signed with the wrong secret — verify_user_jwt rejects,
    # introspection accepts.
    import jwt as _jwt

    bogus_pat = _jwt.encode(
        {"sub": "user-pat", "exp": 9_999_999_999, "token_type": "pat"},
        "different-signing-secret",
        algorithm="HS256",
    )
    with TestClient(app) as client:
        resp = client.get("/me", headers=_bearer(bogus_pat))
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "user-pat"
        assert fake.calls == [bogus_pat]


def test_dispatch_pat_jwt_inactive_returns_401(opaque_settings) -> None:
    fake = _FakeIntrospectionClient(IntrospectionResponse(active=False))
    app = _build_dispatch_app(opaque_settings, fake_client=fake)
    import jwt as _jwt

    bogus_pat = _jwt.encode(
        {"sub": "x", "exp": 9_999_999_999},
        "different-signing-secret",
        algorithm="HS256",
    )
    with TestClient(app) as client:
        resp = client.get("/me", headers=_bearer(bogus_pat))
        assert resp.status_code == 401


def test_dispatch_pat_jwt_no_introspection_client_returns_401(deps_settings) -> None:
    """JWT fails user_jwt verification; introspection unconfigured → 401."""
    app = _build_dispatch_app(deps_settings)
    import jwt as _jwt

    bogus_pat = _jwt.encode(
        {"sub": "x", "exp": 9_999_999_999},
        "different-signing-secret",
        algorithm="HS256",
    )
    with TestClient(app) as client:
        resp = client.get("/me", headers=_bearer(bogus_pat))
        assert resp.status_code == 401


def test_dispatch_non_jwt_garbage_does_not_call_introspection(opaque_settings) -> None:
    """Random non-JWT strings should not waste an introspection call."""
    fake = _FakeIntrospectionClient(IntrospectionResponse(active=False))
    app = _build_dispatch_app(opaque_settings, fake_client=fake)
    with TestClient(app) as client:
        resp = client.get("/me", headers=_bearer("not-a-jwt"))
        assert resp.status_code == 401
        assert fake.calls == []


# ---------------------------------------------------------------------------
# require_permission — permissive mode (openfga_api_url unset)
# ---------------------------------------------------------------------------
@pytest.fixture
def permissive_settings(deps_settings: Settings) -> Settings:
    """deps_settings with OpenFGA *not* deployed — require_permission no-ops."""
    return deps_settings.model_copy(update={"openfga_api_url": ""})


def test_require_permission_permissive_when_openfga_unset(
    permissive_settings, make_user_jwt
) -> None:
    """openfga_api_url='' → require_permission passes any authenticated user,
    even when the (never-called) FGA client would deny."""
    app = _build_app(permissive_settings, fga_check=lambda u, r, o: False)
    with TestClient(app) as client:
        token = make_user_jwt(sub="u-1")
        resp = client.get("/projects/p1", headers=_bearer(token))
        assert resp.status_code == 200


def test_require_permission_permissive_still_requires_auth(permissive_settings) -> None:
    """Permissive mode is not anonymous — an unauthenticated call still 401s."""
    app = _build_app(permissive_settings)
    with TestClient(app) as client:
        resp = client.get("/projects/p1")
        assert resp.status_code == 401


def test_require_permission_enforces_when_openfga_set(deps_settings, make_user_jwt) -> None:
    """openfga_api_url set → the FGA check is real again (regression guard)."""
    app = _build_app(deps_settings, fga_check=lambda u, r, o: False)
    with TestClient(app) as client:
        token = make_user_jwt(sub="u-1")
        resp = client.get("/projects/p1", headers=_bearer(token))
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# require_admin — OpenFGA `admin` relation guard (Tier 3.2)
# ---------------------------------------------------------------------------
def _build_admin_app(
    settings: Settings,
    *,
    principal_dep: Any | None = None,
    fga_check: Callable[..., bool] | None = None,
) -> FastAPI:
    deps_mod.reset_singletons()
    app = FastAPI()

    checks: list[tuple[str, str, str]] = []
    writes: list[tuple[str, str, str]] = []

    class FakeFGA:
        async def check(self, user: str, relation: str, object_: str, **_: Any) -> bool:
            checks.append((user, relation, object_))
            return True if fga_check is None else fga_check(user, relation, object_)

        async def list_objects(self, *a: Any, **k: Any) -> list[str]:
            return []

        async def write_tuple(self, user: str, relation: str, object_: str) -> None:
            writes.append((user, relation, object_))

    fake_fga = FakeFGA()
    app.state.fga_checks = checks  # type: ignore[attr-defined]
    app.state.fga_writes = writes  # type: ignore[attr-defined]
    app.dependency_overrides[deps_mod.get_settings_dep] = lambda: settings
    app.dependency_overrides[deps_mod.get_openfga_client] = lambda: fake_fga
    from backend.shared.authz.deps import require_admin

    @app.get("/admin")
    async def admin_route(_dep: None = Depends(require_admin(principal_dep=principal_dep))) -> dict:
        return {"ok": True}

    return app


def test_require_admin_allows_when_fga_grants_admin(deps_settings, make_user_jwt) -> None:
    """Tier 3.2: require_admin checks the OpenFGA `admin` relation on the
    active tenant — no longer the JWT `app_metadata.role` claim."""
    app = _build_admin_app(deps_settings, fga_check=lambda u, r, o: True)
    with TestClient(app) as client:
        token = make_user_jwt(sub="u-1", active_tenant_id="t-1")
        resp = client.get("/admin", headers=_bearer(token))
        assert resp.status_code == 200
    assert ("user:u-1", "admin", "tenant:t-1") in app.state.fga_checks  # type: ignore[attr-defined]


def test_require_admin_403_when_fga_denies_admin(deps_settings, make_user_jwt) -> None:
    app = _build_admin_app(deps_settings, fga_check=lambda u, r, o: False)
    with TestClient(app) as client:
        token = make_user_jwt(sub="u-1", active_tenant_id="t-1")
        resp = client.get("/admin", headers=_bearer(token))
        assert resp.status_code == 403


def test_require_admin_lazy_provisions_role_tuple(deps_settings, make_user_jwt) -> None:
    """A caller whose JWT still carries `app_metadata.role` drives the same
    lazy tuple-write `require_permission` uses — back-compat during the
    wrapped-JWT cutover, before the raw Supabase JWT (no role) takes over."""
    app = _build_admin_app(deps_settings, fga_check=lambda u, r, o: True)
    with TestClient(app) as client:
        token = make_user_jwt(
            sub="u-1",
            active_tenant_id="t-1",
            extra_claims={"app_metadata": {"role": "owner", "tenant_id": "t-1"}},
        )
        resp = client.get("/admin", headers=_bearer(token))
        assert resp.status_code == 200
    assert ("user:u-1", "owner", "tenant:t-1") in app.state.fga_writes  # type: ignore[attr-defined]


def test_require_admin_403_when_no_active_tenant(deps_settings, user_jwt_secret, issuer) -> None:
    """No active tenant ⇒ no `tenant:<id>` object to check ⇒ 403."""
    import time

    import jwt

    app = _build_admin_app(deps_settings, fga_check=lambda u, r, o: True)
    now_ = int(time.time())
    token = jwt.encode(
        {"iss": issuer, "sub": "u-1", "aud": "bsvibe", "iat": now_, "exp": now_ + 60},
        user_jwt_secret,
        algorithm="HS256",
    )
    with TestClient(app) as client:
        resp = client.get("/admin", headers=_bearer(token))
        assert resp.status_code == 403


def test_require_admin_permissive_when_openfga_unset(permissive_settings, make_user_jwt) -> None:
    """OpenFGA not deployed ⇒ require_admin passes any authenticated caller —
    same posture as require_permission's permissive mode."""
    app = _build_admin_app(permissive_settings, fga_check=lambda u, r, o: False)
    with TestClient(app) as client:
        token = make_user_jwt(sub="u-1", active_tenant_id="t-1")
        resp = client.get("/admin", headers=_bearer(token))
        assert resp.status_code == 200
    assert app.state.fga_checks == []  # type: ignore[attr-defined]


def test_require_admin_401_without_auth(deps_settings) -> None:
    app = _build_admin_app(deps_settings)
    with TestClient(app) as client:
        resp = client.get("/admin")
        assert resp.status_code == 401


def test_require_admin_allows_service_principal(deps_settings, make_service_jwt) -> None:
    """A verified service JWT (scoped to the audience) is an already-authorized
    internal caller — require_admin passes it without an OpenFGA check."""
    from backend.shared.authz.deps import combined_principal

    app = _build_admin_app(deps_settings, principal_dep=combined_principal("bsage"))
    with TestClient(app) as client:
        token = make_service_jwt(sub="service:bsnexus", aud="bsage", tenant_id="t-1")
        resp = client.get("/admin", headers=_bearer(token))
        assert resp.status_code == 200
    assert app.state.fga_checks == []  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# X-Active-Tenant header resolution (Tier 3.2)
# ---------------------------------------------------------------------------
def test_x_active_tenant_header_overrides_jwt_tenant(deps_settings, make_user_jwt) -> None:
    """A valid X-Active-Tenant header (caller is an OpenFGA `member`) becomes
    the effective active tenant, overriding the JWT-carried claim."""
    app = _build_app(deps_settings, fga_check=lambda u, r, o: True)
    with TestClient(app) as client:
        token = make_user_jwt(sub="u-1", active_tenant_id="t-jwt")
        resp = client.get("/me", headers={**_bearer(token), "X-Active-Tenant": "t-header"})
        assert resp.status_code == 200
        assert resp.json()["active_tenant_id"] == "t-header"
    assert ("user:u-1", "member", "tenant:t-header") in app.state.fga_checks  # type: ignore[attr-defined]


def test_x_active_tenant_absent_keeps_jwt_tenant(deps_settings, make_user_jwt) -> None:
    """Absent header ⇒ the JWT-carried active_tenant_id is kept as a
    back-compat fallback (the wrapped-JWT path still works until Phase D)."""
    app = _build_app(deps_settings)
    with TestClient(app) as client:
        token = make_user_jwt(sub="u-1", active_tenant_id="t-jwt")
        resp = client.get("/me", headers=_bearer(token))
        assert resp.status_code == 200
        assert resp.json()["active_tenant_id"] == "t-jwt"
    assert all(r != "member" for _, r, _ in app.state.fga_checks)  # type: ignore[attr-defined]


def test_x_active_tenant_403_when_not_member(deps_settings, make_user_jwt) -> None:
    """Header present but the caller is not an OpenFGA member of it ⇒ 403."""
    app = _build_app(deps_settings, fga_check=lambda u, r, o: False)
    with TestClient(app) as client:
        token = make_user_jwt(sub="u-1", active_tenant_id="t-jwt")
        resp = client.get("/me", headers={**_bearer(token), "X-Active-Tenant": "t-evil"})
        assert resp.status_code == 403


def test_x_active_tenant_permissive_mode_honors_header_without_fga(
    permissive_settings, make_user_jwt
) -> None:
    """OpenFGA unset ⇒ the header is honored without a membership check."""
    app = _build_app(permissive_settings, fga_check=lambda u, r, o: False)
    with TestClient(app) as client:
        token = make_user_jwt(sub="u-1", active_tenant_id="t-jwt")
        resp = client.get("/me", headers={**_bearer(token), "X-Active-Tenant": "t-header"})
        assert resp.status_code == 200
        assert resp.json()["active_tenant_id"] == "t-header"
    assert app.state.fga_checks == []  # type: ignore[attr-defined]


def test_x_active_tenant_flows_into_require_permission(deps_settings, make_user_jwt) -> None:
    """The header-resolved tenant — not the JWT claim — is what a tenant-wide
    require_permission check runs against."""
    app = _build_app(deps_settings, fga_check=lambda u, r, o: True)
    with TestClient(app) as client:
        token = make_user_jwt(sub="u-1", active_tenant_id="t-jwt")
        resp = client.get("/routing", headers={**_bearer(token), "X-Active-Tenant": "t-header"})
        assert resp.status_code == 200
    assert ("user:u-1", "bsgateway_routing_read", "tenant:t-header") in app.state.fga_checks  # type: ignore[attr-defined]


async def test_get_current_user_direct_call_without_fastapi_params(
    deps_settings, make_user_jwt
) -> None:
    """get_current_user is a FastAPI dependency, but products re-wrap it and
    call it *directly* (e.g. BSNexus `core/auth.py::_dispatch_token`). A direct
    caller that omits the FastAPI-injected ``x_active_tenant`` / ``fga`` params
    receives the unresolved ``Header()`` / ``Depends()`` sentinels as values —
    the ``Header`` sentinel is truthy, so without normalisation it would drive
    the X-Active-Tenant branch into ``<Depends>.check`` and AttributeError.
    The library must skip the header path for direct callers, not crash."""
    deps_mod.reset_singletons()
    token = make_user_jwt(sub="u-1", active_tenant_id="t-jwt")
    user = await deps_mod.get_current_user(
        authorization=f"Bearer {token}",
        settings=deps_settings,
        introspection_client=None,
        introspection_cache=IntrospectionCache(ttl_s=30),
    )
    assert user.id == "u-1"
    assert user.active_tenant_id == "t-jwt"


def test_get_current_user_verifies_raw_supabase_es256_jwt(monkeypatch) -> None:
    """Tier 3.2 collapse path: get_current_user verifies a raw Supabase-shaped
    ES256 JWT via JWKS (aud=authenticated) — no wrapped HS256 layer, and no
    tenant claim, so active_tenant_id is None until an X-Active-Tenant header
    supplies it."""
    import time

    import jwt as _pyjwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from backend.shared.authz.auth import reset_jwks_cache

    private = ec.generate_private_key(ec.SECP256R1())
    priv_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    class _StubKey:
        def __init__(self, key: bytes) -> None:
            self.key = key

    class _StubJWKSClient:
        def __init__(self, *_a: Any, **_k: Any) -> None: ...

        def get_signing_key_from_jwt(self, _t: str) -> _StubKey:
            return _StubKey(pub_pem)

    monkeypatch.setattr(_pyjwt, "PyJWKClient", _StubJWKSClient)
    reset_jwks_cache()

    settings = Settings(  # type: ignore[call-arg]
        bsvibe_auth_url="https://auth.bsvibe.dev",
        openfga_api_url="http://openfga.local:8080",
        openfga_store_id="store-1",
        openfga_auth_model_id="model-1",
        service_token_signing_secret="x",
        user_jwt_jwks_url="https://proj.supabase.co/auth/v1/.well-known/jwks.json",
        user_jwt_algorithm="ES256",
        user_jwt_audience="authenticated",
    )
    app = _build_app(settings)
    now_ = int(time.time())
    token = _pyjwt.encode(
        {
            "iss": "https://proj.supabase.co/auth/v1",
            "sub": "00000000-0000-0000-0000-0000000000ab",
            "email": "founder@bsvibe.dev",
            "aud": "authenticated",
            "iat": now_,
            "exp": now_ + 3600,
        },
        priv_pem,
        algorithm="ES256",
        headers={"kid": "supabase-key-1"},
    )
    with TestClient(app) as client:
        resp = client.get("/me", headers=_bearer(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "00000000-0000-0000-0000-0000000000ab"
        assert body["email"] == "founder@bsvibe.dev"
        assert body["active_tenant_id"] is None


# ---------------------------------------------------------------------------
# combined_principal — service JWT OR user dispatch on the same route
# ---------------------------------------------------------------------------
def _build_combined_app(settings: Settings, audience: str = "bsage") -> FastAPI:
    deps_mod.reset_singletons()
    app = FastAPI()
    app.dependency_overrides[deps_mod.get_settings_dep] = lambda: settings
    from backend.shared.authz.deps import combined_principal

    @app.get("/who")
    async def who(user: User = Depends(combined_principal(audience))) -> dict:
        return {"id": user.id, "is_service": user.is_service}

    return app


def test_combined_principal_resolves_service_jwt(deps_settings, make_service_jwt) -> None:
    app = _build_combined_app(deps_settings, audience="bsage")
    with TestClient(app) as client:
        token = make_service_jwt(sub="service:bsnexus", aud="bsage", tenant_id="t-1")
        resp = client.get("/who", headers=_bearer(token))
        assert resp.status_code == 200
        assert resp.json() == {"id": "service:bsnexus", "is_service": True}


def test_combined_principal_falls_through_to_user_jwt(deps_settings, make_user_jwt) -> None:
    app = _build_combined_app(deps_settings, audience="bsage")
    with TestClient(app) as client:
        token = make_user_jwt(sub="u-1")
        resp = client.get("/who", headers=_bearer(token))
        assert resp.status_code == 200
        assert resp.json() == {"id": "u-1", "is_service": False}


def test_combined_principal_rejects_wrong_service_audience(deps_settings, make_service_jwt) -> None:
    """A service JWT for a different audience is not a valid user JWT either
    → falls through to get_current_user, which 401s."""
    app = _build_combined_app(deps_settings, audience="bsage")
    with TestClient(app) as client:
        token = make_service_jwt(sub="service:bsnexus", aud="bsgateway")
        resp = client.get("/who", headers=_bearer(token))
        assert resp.status_code == 401


def test_combined_principal_401_without_auth(deps_settings) -> None:
    app = _build_combined_app(deps_settings, audience="bsage")
    with TestClient(app) as client:
        resp = client.get("/who")
        assert resp.status_code == 401


# ---- lazy singleton sanity -------------------------------------------------


def test_get_introspection_cache_singleton(opaque_settings) -> None:
    deps_mod.reset_singletons()
    a = deps_mod.get_introspection_cache(opaque_settings)
    b = deps_mod.get_introspection_cache(opaque_settings)
    assert a is b


def test_get_introspection_client_singleton(opaque_settings) -> None:
    deps_mod.reset_singletons()
    a = deps_mod.get_introspection_client(opaque_settings)
    b = deps_mod.get_introspection_client(opaque_settings)
    assert a is b


def test_get_introspection_client_returns_none_when_disabled(deps_settings) -> None:
    deps_mod.reset_singletons()
    assert deps_mod.get_introspection_client(deps_settings) is None


# ---------------------------------------------------------------------------
# check_permission / check_tenant_permission — shared core (Tier 5 Phase 3,
# used by both require_permission and the MCP tool dispatcher)
# ---------------------------------------------------------------------------
class _FakeFGACore:
    """Minimal FGA double recording checks + writes for the shared-core tests."""

    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.checks: list[tuple[str, str, str]] = []
        self.writes: list[tuple[str, str, str]] = []

    async def check(self, user: str, relation: str, object_: str, **_: object) -> bool:
        self.checks.append((user, relation, object_))
        return self.allow

    async def list_objects(self, *a: object, **k: object) -> list[str]:
        return []

    async def write_tuple(self, user: str, relation: str, object_: str) -> None:
        self.writes.append((user, relation, object_))


def _core_pieces(deps_settings: Settings):
    from backend.shared.authz.cache import PermissionCache

    return _FakeFGACore(), PermissionCache(ttl_s=30)


async def test_check_permission_returns_fga_decision(deps_settings) -> None:
    from backend.shared.authz import check_permission

    fga, cache = _core_pieces(deps_settings)
    fga.allow = True
    user = User(id="u-1", active_tenant_id="t-1", app_metadata={"role": "member"})
    allowed = await check_permission(
        user=user,
        relation="bsgateway_routing_read",
        object_="tenant:t-1",
        fga=fga,
        cache=cache,
        settings=deps_settings,
    )
    assert allowed is True
    assert ("user:u-1", "bsgateway_routing_read", "tenant:t-1") in fga.checks
    # lazy-provisions the role tuple from app_metadata.role
    assert ("user:u-1", "member", "tenant:t-1") in fga.writes


async def test_check_permission_denies_when_fga_denies(deps_settings) -> None:
    from backend.shared.authz import check_permission

    fga, cache = _core_pieces(deps_settings)
    fga.allow = False
    user = User(id="u-1", active_tenant_id="t-1")
    allowed = await check_permission(
        user=user,
        relation="bsgateway_routing_write",
        object_="tenant:t-1",
        fga=fga,
        cache=cache,
        settings=deps_settings,
    )
    assert allowed is False


async def test_check_permission_permissive_when_openfga_unset(deps_settings) -> None:
    from backend.shared.authz import check_permission

    fga, cache = _core_pieces(deps_settings)
    fga.allow = False
    permissive = deps_settings.model_copy(update={"openfga_api_url": ""})
    user = User(id="u-1", active_tenant_id="t-1")
    allowed = await check_permission(
        user=user,
        relation="bsgateway_routing_read",
        object_="tenant:t-1",
        fga=fga,
        cache=cache,
        settings=permissive,
    )
    assert allowed is True
    assert fga.checks == []  # OpenFGA never called


async def test_check_tenant_permission_builds_triple_relation(deps_settings) -> None:
    from backend.shared.authz import check_tenant_permission

    fga, cache = _core_pieces(deps_settings)
    user = User(id="u-1", active_tenant_id="t-1", app_metadata={"role": "admin"})
    allowed = await check_tenant_permission(
        user, "bsupervisor.incidents.read", fga=fga, cache=cache, settings=deps_settings
    )
    assert allowed is True
    assert ("user:u-1", "bsupervisor_incidents_read", "tenant:t-1") in fga.checks


async def test_check_tenant_permission_false_without_active_tenant(deps_settings) -> None:
    from backend.shared.authz import check_tenant_permission

    fga, cache = _core_pieces(deps_settings)
    user = User(id="u-1", active_tenant_id=None)
    allowed = await check_tenant_permission(
        user, "bsage.vault.read", fga=fga, cache=cache, settings=deps_settings
    )
    assert allowed is False
    assert fga.checks == []


async def test_check_tenant_permission_rejects_malformed_permission(deps_settings) -> None:
    from backend.shared.authz import check_tenant_permission

    fga, cache = _core_pieces(deps_settings)
    user = User(id="u-1", active_tenant_id="t-1")
    with pytest.raises(ValueError, match="invalid permission"):
        await check_tenant_permission(
            user, "bad.permission", fga=fga, cache=cache, settings=deps_settings
        )
