"""[P] #959 — the anchor-backfill CLI asks for its cross-tenant read by name.

``bootstrap_anchor_backfill`` is an operator one-shot: it picks every product
whose bootstrap completed — filtered by slug or workspace, or all of them — and
retrofits each workspace's vault. The pick joins ``products`` and ``workspaces``,
both RLS-forced, and ran with an EMPTY GUC (full-suite probe, 2026-09-29).
Under the fail-closed policy (#959 ③) it would find nothing and report success.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.identity.workspaces_db import ProductRow
from backend.workflow.application.runtime.bootstrap_anchor_backfill import _resolve_targets

from .conftest import PoliciedRead, bootstrap_tenant, requires_real_pg

pytestmark = [pytest.mark.asyncio, requires_real_pg]


async def test_the_backfill_picks_every_tenants_products(
    session_factory: async_sessionmaker[AsyncSession],
    policied_reads: list[PoliciedRead],
) -> None:
    expected = set()
    for email in ("a@x.io", "b@x.io"):
        ws = await bootstrap_tenant(
            session_factory, supabase_user_id=f"bf-{uuid.uuid4()}", email=email
        )
        pid = uuid.uuid4()
        async with session_factory() as session:
            async with workspace_session_scope(session, ws):
                session.add(
                    ProductRow(
                        id=pid,
                        workspace_id=ws,
                        name="p",
                        slug=f"p-{pid.hex[:12]}",
                        bootstrap_status="complete",
                    )
                )
                await session.flush()
            await session.commit()
        expected.add((ws, pid))
    policied_reads.clear()

    targets = await _resolve_targets(session_factory, product_slug=None, workspace_id=None)

    assert {(t.workspace_id, t.product_id) for t in targets} == expected
    assert any(g == "*" for _, g in policied_reads), policied_reads
    assert [r for r in policied_reads if r[1] == ""] == []
