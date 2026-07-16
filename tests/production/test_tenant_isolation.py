"""Tenant isolation, proven end-to-end through the REAL app (INV-3).

Two tenants are bootstrapped through the production bootstrap service, each
mints a real JWT (verified by the real ``get_current_user``), and drives real
HTTP routes. The proof spans the two DEFENCE LAYERS, kept explicitly distinct:

* **Layer 3 — Postgres RLS** on an RLS table (``products``): the runtime
  connects as the NON-superuser role ``bsvibe_app`` (B2b two-role setup), so
  ``ENABLE`` + ``FORCE ROW LEVEL SECURITY`` actually govern it. Test #1 proves
  isolation through the API; test #3 proves the RLS policy is the ACTIVE filter
  by reading with RAW SQL (ORM auto-filter out of the picture) directly on the
  ``bsvibe_app`` connection — no ``SET ROLE`` hack.
* **Layer 2 — ORM auto-filter** on an app-filter-only table
  (``model_accounts``, NOT in the RLS set): here the ORM auto-filter / explicit
  ``workspace_id`` predicate is the ONLY defence, and test #2 pins it down.

Test #3 also documents WHY ``get_workspace_id`` is load-bearing: the RLS policy
is fail-open when the GUC is unset, so the per-request GUC set is what turns the
policy from "visible" to "isolated". The migration alone does not isolate.
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
# 3. RLS is the ACTIVE layer-3 defense for the real runtime role (B2b).
#    Proven on the ACTUAL ``bsvibe_app`` connection — no SET ROLE hack.
# ---------------------------------------------------------------------------
async def test_rls_is_active_layer3_for_the_runtime_role(
    real_app: object, session_factory: async_sessionmaker
) -> None:
    """The RLS policy — not the ORM filter — isolates ``products`` for the
    real runtime role.

    The B2b two-role setup means the app (and this fixture's ``session_factory``,
    which is the production ``deps`` singleton) connects as ``bsvibe_app``: a
    ``NOSUPERUSER NOBYPASSRLS`` role. So we can prove RLS on the ACTUAL runtime
    connection with no ``SET ROLE`` workaround. Three facts, all load-bearing:

    1. **The runtime role is governed.** ``current_user`` is NOT a superuser and
       NOT ``BYPASSRLS`` — the precondition for RLS to bite at all. If this ever
       regresses to a superuser DSN, the assertion fails loudly rather than the
       isolation silently degrading to layer-2-only.
    2. **Fail-open on unset GUC.** With the GUC empty the ``products`` policy
       (``current_setting IS NULL OR '' OR workspace_id = GUC``) returns EVERY
       tenant's rows. That is why ``get_workspace_id`` — which sets the GUC on
       every request — is load-bearing; the migration alone does not isolate.
    3. **GUC-scoped read is isolated by RLS.** With the GUC = tenant A, a RAW
       SQL read (ORM auto-filter out of the picture) returns ONLY tenant A —
       the DATABASE, not the app, hid tenant B.
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

        # (1) The precondition: the runtime role RLS is supposed to govern.
        is_super = (
            await conn.execute(
                text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).scalar_one()
        assert is_super is False, (
            "RLS is INERT for a superuser / BYPASSRLS role — the runtime must "
            "connect as the least-privilege bsvibe_app role for layer-3 to bite. "
            "current_user is a superuser; the B2b cutover did not take effect."
        )

        # (2) GUC UNSET (empty) → RLS is fail-open → EVERY tenant's rows.
        await conn.execute(text(f"SELECT set_config('{_GUC}', '', false)"))
        unscoped = (await conn.execute(text("SELECT workspace_id FROM products"))).all()
        unscoped_ws = {row[0] for row in unscoped}
        assert a_ws in unscoped_ws and b_ws in unscoped_ws, (
            "fail-open expectation broken: with the GUC unset the RLS policy "
            f"should expose cross-tenant rows (got {unscoped_ws})"
        )

        # (3) GUC = tenant A → RLS returns ONLY tenant A's rows. Raw SQL, so the
        # ORM auto-filter (layer 2) is not involved — this is RLS alone.
        await conn.execute(text(f"SELECT set_config('{_GUC}', :ws, false)"), {"ws": str(a_ws)})
        scoped = (await conn.execute(text("SELECT workspace_id FROM products"))).all()
        scoped_ws = {row[0] for row in scoped}
        assert scoped_ws == {a_ws}, (
            f"GUC-scoped RLS read must return ONLY tenant A; got {scoped_ws}. "
            "The RLS policy (layer 3) — not the ORM filter — must isolate here."
        )
        # Reset the pooled connection's GUC so a later fixture reusing it fails
        # open rather than tripping an RLS WITH CHECK.
        await conn.execute(text(f"SELECT set_config('{_GUC}', '', false)"))
