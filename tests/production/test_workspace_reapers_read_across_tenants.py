"""[P] #959 ② — the disk reapers read every tenant's runs and products by name.

``AgentWorker._reap_terminal_run_workspaces`` runs three sweeps that decide what
to DELETE from ``var/runs`` and ``var/products`` by asking ``execution_runs`` and
``products`` — both RLS-forced — which of the dirs on disk are still alive:

* ``reap_terminal_run_workspaces`` — a dir with no visible run row is an orphan
* ``reap_orphan_product_workspaces`` — a dir with no visible product row is dead
* ``reap_idle_product_workspaces`` — a product with a visible live run is busy

The probe caught them reading with an EMPTY GUC. Fail-open today; under the
fail-closed policy (#959 ③) every row is invisible, so every day-old run dir
reads as an orphan and every product as dead — the sweep would delete live work.

Measured as the enumeration test does: no policied SELECT runs with an empty
GUC, and the reads this proposition is about were seen carrying ``'*'``.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import get_settings
from backend.data.rls import workspace_session_scope
from backend.identity.workspaces_db import ProductRow
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.workers.agent_worker import AgentWorker

from .conftest import PoliciedRead, bootstrap_tenant, requires_real_pg

pytestmark = [pytest.mark.asyncio, requires_real_pg]

_DAY_AGO = datetime.now(tz=UTC) - timedelta(days=2)


def _old_dir(root: Path, name: uuid.UUID) -> None:
    d = root / str(name)
    d.mkdir(parents=True)
    os.utime(d, (_DAY_AGO.timestamp(), _DAY_AGO.timestamp()))


async def _live_product_with_running_run(
    factory: async_sessionmaker[AsyncSession], ws: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    product_id, run_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as session:
        async with workspace_session_scope(session, ws):
            session.add(
                ProductRow(
                    id=product_id,
                    workspace_id=ws,
                    name=f"p-{product_id.hex[:6]}",
                    slug=f"p-{product_id.hex[:12]}",
                    created_at=_DAY_AGO,
                    updated_at=_DAY_AGO,
                )
            )
            await session.flush()
            session.add(
                ExecutionRun(
                    id=run_id,
                    workspace_id=ws,
                    product_id=product_id,
                    status=RunStatus.RUNNING,
                    payload={},
                    created_at=_DAY_AGO,
                    updated_at=_DAY_AGO,
                )
            )
            await session.flush()
        await session.commit()
    return product_id, run_id


async def test_the_reapers_read_runs_and_products_across_tenants(
    session_factory: async_sessionmaker[AsyncSession],
    policied_reads: list[PoliciedRead],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_root, products_root = tmp_path / "runs", tmp_path / "products"
    monkeypatch.setattr(get_settings(), "run_workspace_root", str(runs_root))
    monkeypatch.setattr(get_settings(), "product_workspace_root", str(products_root))

    for email in ("a@x.io", "b@x.io"):
        ws = await bootstrap_tenant(
            session_factory, supabase_user_id=f"r-{uuid.uuid4()}", email=email
        )
        product_id, run_id = await _live_product_with_running_run(session_factory, ws)
        _old_dir(runs_root, run_id)
        _old_dir(products_root, product_id)

    policied_reads.clear()
    assert await AgentWorker(session_factory=session_factory)._reap_terminal_run_workspaces() == 0

    # Live work survived (fail-open today — this alone cannot flip; the GUC can).
    assert len(list(runs_root.iterdir())) == 2
    assert len(list(products_root.iterdir())) == 2

    starred = {t for tables, guc in policied_reads if guc == "*" for t in tables}
    assert {"execution_runs", "products"} <= starred, policied_reads
    assert [r for r in policied_reads if r[1] == ""] == []
