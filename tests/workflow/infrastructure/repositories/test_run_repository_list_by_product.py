"""SqlAlchemyRunRepository.list_by_product — PostgreSQL-backed query test.

This exercises product_id filtering + created_at ordering + limit, which is
exactly the kind of query SQLite is too lax to validate faithfully (types /
FKs / index behaviour). CI runs on PostgreSQL and has twice been bitten by
SQLite-only coverage, so this test MUST run against the real PG container
(``citest-pg`` @ localhost:5442, ``bsvibe``/``bsvibe``). It skips loudly when
PG is unreachable rather than silently degrading to SQLite.

FK PARENT ORDER on PG: workspace row → product row → execution_run rows.
Inserting runs before their workspace/product parents fails the FK on PG.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

# Register cross-domain tables (workspaces/products) on the shared Base.metadata.
import backend.identity.workspaces_db  # noqa: F401
from backend.identity.workspaces_db import ProductRow, WorkspaceRow
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.repositories import SqlAlchemyRunRepository
from tests._support import db_engine, use_real_pg

pytestmark = pytest.mark.asyncio


async def test_list_by_product_pg_filters_orders_and_limits() -> None:
    if not use_real_pg():
        pytest.skip(
            "list_by_product must be validated on real PostgreSQL (citest-pg @ "
            "localhost:5442). Set BSVIBE_DATABASE_URL and start the container."
        )

    async with db_engine() as (engine, is_pg):
        assert is_pg, "this test only runs against PostgreSQL"
        maker = async_sessionmaker(engine, expire_on_commit=False)

        workspace_id = uuid.uuid4()
        product_id = uuid.uuid4()
        other_product_id = uuid.uuid4()
        now = datetime.now(tz=UTC)

        async with maker() as session:
            # FK parents first: workspace → products → runs.
            session.add(WorkspaceRow(id=workspace_id, name="Acme", language="en"))
            await session.flush()
            session.add(
                ProductRow(
                    id=product_id,
                    workspace_id=workspace_id,
                    name="Target",
                    slug=f"target-{product_id.hex[:8]}",
                    product_metadata={},
                )
            )
            session.add(
                ProductRow(
                    id=other_product_id,
                    workspace_id=workspace_id,
                    name="Other",
                    slug=f"other-{other_product_id.hex[:8]}",
                    product_metadata={},
                )
            )
            await session.flush()

            repo = SqlAlchemyRunRepository(session)
            target_ids: list[uuid.UUID] = []
            for i in range(3):
                run_id = uuid.uuid4()
                target_ids.append(run_id)
                await repo.add(
                    ExecutionRun(
                        id=run_id,
                        workspace_id=workspace_id,
                        product_id=product_id,
                        status=RunStatus.SHIPPED,
                        payload={"intent_text": f"run {i}"},
                        created_at=now - timedelta(minutes=3 - i),  # i=2 newest
                        updated_at=now,
                    )
                )
            # A run for a DIFFERENT product in the same workspace → must be excluded.
            await repo.add(
                ExecutionRun(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    product_id=other_product_id,
                    status=RunStatus.SHIPPED,
                    payload={"intent_text": "other product run"},
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()

        async with maker() as session:
            repo = SqlAlchemyRunRepository(session)
            rows = await repo.list_by_product(workspace_id, product_id)

            # Only THIS product's runs, newest-first.
            assert {r.product_id for r in rows} == {product_id}
            assert len(rows) == 3
            assert rows[0].id == target_ids[2]  # newest
            assert rows[2].id == target_ids[0]  # oldest

            # Limit is honoured.
            limited = await repo.list_by_product(workspace_id, product_id, limit=2)
            assert len(limited) == 2
            assert limited[0].id == target_ids[2]
