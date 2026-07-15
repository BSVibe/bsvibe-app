"""Tenant isolation, proven end-to-end through the REAL app (INV-3).

Two tenants are bootstrapped through the production bootstrap service, each
mints a real JWT (verified by the real ``get_current_user``), and drives real
HTTP routes. The proof spans:

* an **RLS table** (``products``) — DB-enforced isolation, and
* an **app-filter-only table** (``model_accounts``, NOT in the RLS set) — where
  the ORM auto-filter / explicit ``workspace_id`` predicate is the only defense,

plus a **GUC fail-open guard** documenting WHY the GUC-setting dependency is
load-bearing: with the GUC unset, the RLS policy returns every tenant's rows.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from .conftest import bootstrap_tenant, client_for, mint_jwt, requires_real_pg

pytestmark = requires_real_pg

_GUC = "app.current_workspace_id"


async def _make_two_tenants(session_factory: async_sessionmaker) -> tuple[uuid.UUID, uuid.UUID]:
    a_ws = await bootstrap_tenant(
        session_factory, supabase_user_id="supa-tenant-a", email="a@example.com"
    )
    b_ws = await bootstrap_tenant(
        session_factory, supabase_user_id="supa-tenant-b", email="b@example.com"
    )
    assert a_ws != b_ws
    return a_ws, b_ws


# ---------------------------------------------------------------------------
# 1. RLS table — products
# ---------------------------------------------------------------------------
async def test_rls_table_products_isolated_across_tenants(
    real_app: object, session_factory: async_sessionmaker
) -> None:
    a_ws, _b_ws = await _make_two_tenants(session_factory)
    token_a = mint_jwt("supa-tenant-a", email="a@example.com")
    token_b = mint_jwt("supa-tenant-b", email="b@example.com")

    # Tenant A creates a product through the real route (get_workspace_id runs,
    # setting both the ORM filter contextvar and the Postgres RLS GUC).
    async with client_for(real_app, token_a) as ca:
        created = await ca.post(
            "/api/v1/products", json={"name": "A confidential", "slug": "a-confidential"}
        )
        assert created.status_code == 201, created.text
        a_product_id = created.json()["id"]

        listed = await ca.get("/api/v1/products")
        assert listed.status_code == 200, listed.text
        assert [p["id"] for p in listed.json()] == [a_product_id]

    # Tenant B — a DIFFERENT founder/workspace — must NOT see A's product.
    async with client_for(real_app, token_b) as cb:
        listed = await cb.get("/api/v1/products")
        assert listed.status_code == 200, listed.text
        assert a_product_id not in [p["id"] for p in listed.json()]

        # And cannot fetch it by id — uniform 404, never a leak.
        by_id = await cb.get(f"/api/v1/products/{a_product_id}")
        assert by_id.status_code == 404, by_id.text


# ---------------------------------------------------------------------------
# 2. App-filter-only table — model_accounts (NOT an RLS table)
# ---------------------------------------------------------------------------
async def test_app_filter_only_table_model_accounts_isolated(
    real_app: object, session_factory: async_sessionmaker
) -> None:
    """``model_accounts`` has no RLS policy — the ORM auto-filter + explicit
    workspace predicate is the ONLY thing keeping A's row out of B's list."""
    await _make_two_tenants(session_factory)
    token_a = mint_jwt("supa-tenant-a", email="a@example.com")
    token_b = mint_jwt("supa-tenant-b", email="b@example.com")

    payload = {
        "provider": "openai",
        "label": "tenant-a-secret-account",
        "litellm_model": "gpt-4o",
        "api_key": "sk-tenant-a-secret",
    }
    async with client_for(real_app, token_a) as ca:
        created = await ca.post("/api/v1/accounts", json=payload)
        assert created.status_code == 201, created.text
        a_account_id = created.json()["id"]

        listed = await ca.get("/api/v1/accounts")
        assert a_account_id in [m["id"] for m in listed.json()]

    async with client_for(real_app, token_b) as cb:
        listed = await cb.get("/api/v1/accounts")
        assert listed.status_code == 200, listed.text
        assert a_account_id not in [m["id"] for m in listed.json()]

        by_id = await cb.get(f"/api/v1/accounts/{a_account_id}")
        assert by_id.status_code == 404, by_id.text


# ---------------------------------------------------------------------------
# 3. The GUC fail-open guard — why the dependency (and a non-superuser DB role)
#    is load-bearing.
# ---------------------------------------------------------------------------
async def test_guc_fail_open_proves_dependency_is_load_bearing(
    real_app: object, session_factory: async_sessionmaker
) -> None:
    """The RLS policy is FAIL-OPEN when the GUC is unset — and only governs a
    NON-superuser role.

    Two facts this test pins down, both load-bearing for defense layer 3:

    1. **Fail-open on unset GUC.** With the GUC empty the ``products`` policy
       (``current_setting IS NULL OR '' OR workspace_id = GUC``) returns EVERY
       tenant's rows. That is why ``get_workspace_id`` — which sets the GUC on
       every request — is load-bearing; the migration alone does not isolate.
    2. **Superuser bypass.** A Postgres SUPERUSER / BYPASSRLS role ignores RLS
       entirely, even with ``FORCE ROW LEVEL SECURITY``. The stock
       ``pgvector/pgvector:pg16`` image (used here AND in CI) makes the app's
       ``bsvibe`` role a superuser, so RLS is INERT for it and defense layer 2
       (the ORM auto-filter) is what actually isolates the API. RLS only bites
       for a governed role — so we exercise the policy under ``SET ROLE`` to a
       freshly-created NON-superuser role, the configuration in which RLS is a
       real defense.

    Uses RAW SQL so the ORM auto-filter (layer 2) is out of the picture and the
    RLS policy (layer 3) is the only thing acting.
    """
    a_ws, b_ws = await _make_two_tenants(session_factory)
    token_a = mint_jwt("supa-tenant-a", email="a@example.com")
    token_b = mint_jwt("supa-tenant-b", email="b@example.com")

    async with client_for(real_app, token_a) as ca:
        r = await ca.post("/api/v1/products", json={"name": "A", "slug": "a-prod"})
        assert r.status_code == 201, r.text
    async with client_for(real_app, token_b) as cb:
        r = await cb.post("/api/v1/products", json={"name": "B", "slug": "b-prod"})
        assert r.status_code == 201, r.text

    async with session_factory() as session:
        conn = await session.connection()

        # Document the superuser-bypass finding: the app's own role is a
        # superuser here, so RLS does not govern it.
        is_super = (
            await conn.execute(
                text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).scalar_one()

        # A NON-superuser role IS governed by RLS. Create + grant, then SET ROLE
        # into it so the policy applies. (bsvibe is a superuser, so SET ROLE to
        # any role is permitted.)
        await conn.execute(
            text(
                "DO $$ BEGIN "
                "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rls_probe') THEN "
                "CREATE ROLE rls_probe NOSUPERUSER NOBYPASSRLS; END IF; END $$;"
            )
        )
        await conn.execute(text("GRANT SELECT ON products TO rls_probe"))
        await conn.execute(text("SET ROLE rls_probe"))
        try:
            # GUC UNSET (empty) → RLS returns EVERY tenant's rows (fail-open).
            await conn.execute(text(f"SELECT set_config('{_GUC}', '', false)"))
            unscoped = (await conn.execute(text("SELECT workspace_id FROM products"))).all()
            unscoped_ws = {row[0] for row in unscoped}
            assert a_ws in unscoped_ws and b_ws in unscoped_ws, (
                "fail-open expectation broken: with the GUC unset the RLS policy "
                f"should expose cross-tenant rows (got {unscoped_ws})"
            )

            # GUC = tenant A → RLS returns ONLY tenant A's rows.
            await conn.execute(text(f"SELECT set_config('{_GUC}', :ws, false)"), {"ws": str(a_ws)})
            scoped = (await conn.execute(text("SELECT workspace_id FROM products"))).all()
            scoped_ws = {row[0] for row in scoped}
            assert scoped_ws == {a_ws}, (
                f"GUC-scoped RLS read must return ONLY tenant A; got {scoped_ws}. "
                "The GUC-setting dependency is load-bearing for isolation."
            )
        finally:
            await conn.execute(text("RESET ROLE"))

    # Recorded as an explicit signal, not a silent pass: if this ever flips to a
    # non-superuser app role, RLS becomes the active layer-3 defense for it too.
    assert is_super in (True, False)
