"""[P] #959 — the plural /api/v1/workspaces router publishes the workspace it touches.

Every other v1 router resolves ONE workspace through ``get_workspace_id``, which
publishes it as the RLS GUC. This one is the multi-workspace surface: it takes
no ``get_workspace_id`` (the caller may name any workspace they belong to), so
its request GUC stayed EMPTY. Fail-open hid that; under the fail-closed policy
(#959 ③) the list comes back empty, a path-addressed workspace reads as 404,
and a PATCH/DELETE commit matches nothing — all without an error.

* the list is a cross-tenant read, narrowed by the caller's own memberships
* a path-addressed route publishes the path workspace once membership is proven

Measured on the real app with real auth: every policied read runs under a
non-empty GUC, every write under the row's own workspace.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    PoliciedRead,
    PoliciedWrite,
    bootstrap_tenant,
    client_for,
    mint_jwt,
    requires_real_pg,
)

pytestmark = [pytest.mark.asyncio, requires_real_pg]


async def test_list_patch_and_delete_run_under_the_workspace_they_touch(
    real_app: object,
    session_factory: async_sessionmaker[AsyncSession],
    policied_reads: list[PoliciedRead],
    policied_writes: list[PoliciedWrite],
) -> None:
    sub = f"ws-router-{uuid.uuid4()}"
    first = await bootstrap_tenant(session_factory, supabase_user_id=sub, email="w@x.io")
    token = mint_jwt(sub, email="w@x.io")

    async with client_for(real_app, token) as client:
        created = await client.post("/api/v1/workspaces", json={"name": "second"})
        assert created.status_code == 201, created.text
        second = uuid.UUID(created.json()["id"])

        policied_reads.clear()
        policied_writes.clear()

        listed = await client.get("/api/v1/workspaces")
        assert listed.status_code == 200, listed.text
        assert {uuid.UUID(w["id"]) for w in listed.json()} == {first, second}

        got = await client.get(f"/api/v1/workspaces/{second}")
        assert got.status_code == 200, got.text

        patched = await client.patch(f"/api/v1/workspaces/{second}", json={"name": "renamed"})
        assert patched.status_code == 200, patched.text
        assert patched.json()["name"] == "renamed"

        deleted = await client.delete(f"/api/v1/workspaces/{second}")
        assert deleted.status_code == 204, deleted.text

    reads, writes = list(policied_reads), list(policied_writes)
    assert reads, "positive control: the routes read policied rows"
    assert [r for r in reads if r[1] == ""] == []
    assert {ws for t, ws, _ in writes if t == "workspaces"} == {str(second)}
    assert [w for w in writes if w[1] != w[2]] == []
