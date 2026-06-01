"""ResourceBindingRepository — the per-Product × Connector 3-knob binding.

A Resource (Workflow §3) is the binding that carries **selection**, **trigger
{enabled, filters}**, and **output_mode {safe|direct}** for one Product against
one ConnectorAccount + a connector-side ``resource_id``. The repository is
workspace-scoped; ``find_binding(connector_account_id, resource_id)`` is the
lookup the Receive stage (B10b) will use to resolve an inbound webhook back to a
Product/Resource.

FK-safe seeding: every test creates + flushes its Workspace + Product +
ConnectorAccount parents BEFORE inserting a binding (real Postgres enforces the
FKs; SQLite does not — this bit prior PRs).
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.connectors.db import ConnectorAccountRow
from backend.identity.infrastructure.repositories import SqlAlchemyResourceBindingRepository
from backend.identity.workspaces_db import ProductRow, WorkspaceRow, WorkspacesBase
from tests._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine(WorkspacesBase) as (engine, _is_pg):
        # ConnectorAccountRow registers on the shared Base; ensure its table
        # exists too (db_engine create_all covers imported models).
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed_parents(
    sf: async_sessionmaker, *, workspace_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create + flush a Product + a ConnectorAccount under ``workspace_id``.

    Returns ``(product_id, connector_account_id)``.
    """
    product_id = uuid.uuid4()
    connector_account_id = uuid.uuid4()
    async with sf() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1", safe_mode=True))
        await s.flush()
        s.add(ProductRow(id=product_id, workspace_id=workspace_id, name="Blog", slug="blog"))
        s.add(
            ConnectorAccountRow(
                id=connector_account_id,
                workspace_id=workspace_id,
                connector="github",
                webhook_token=f"tok-{uuid.uuid4().hex}",
                signing_secret_ciphertext="cipher",
            )
        )
        await s.commit()
    return product_id, connector_account_id


async def test_create_persists_three_knobs(sf) -> None:
    ws = uuid.uuid4()
    product_id, conn_id = await _seed_parents(sf, workspace_id=ws)

    async with sf() as s:
        repo = SqlAlchemyResourceBindingRepository(s)
        row = await repo.create(
            workspace_id=ws,
            product_id=product_id,
            connector_account_id=conn_id,
            resource_id="bsvibe/bsvibe-site",
            selection={"labels": ["bug"]},
            trigger={"enabled": True, "filters": {"branch": "main"}},
            output_mode="direct",
        )
        await s.commit()
        binding_id = row.id

    async with sf() as s:
        repo = SqlAlchemyResourceBindingRepository(s)
        got = await repo.get(workspace_id=ws, binding_id=binding_id)
        assert got is not None
        assert got.product_id == product_id
        assert got.connector_account_id == conn_id
        assert got.resource_id == "bsvibe/bsvibe-site"
        assert got.selection == {"labels": ["bug"]}
        assert got.trigger == {"enabled": True, "filters": {"branch": "main"}}
        assert got.output_mode == "direct"


async def test_create_defaults_safe_and_disabled_trigger(sf) -> None:
    ws = uuid.uuid4()
    product_id, conn_id = await _seed_parents(sf, workspace_id=ws)

    async with sf() as s:
        repo = SqlAlchemyResourceBindingRepository(s)
        row = await repo.create(
            workspace_id=ws,
            product_id=product_id,
            connector_account_id=conn_id,
            resource_id="repo#1",
        )
        await s.commit()
        # Defaults per spec: output_mode 'safe', trigger disabled with no filters.
        assert row.output_mode == "safe"
        assert row.trigger == {"enabled": False, "filters": {}}
        assert row.selection == {}


async def test_list_by_product_scoped_to_workspace(sf) -> None:
    ws = uuid.uuid4()
    product_id, conn_id = await _seed_parents(sf, workspace_id=ws)

    async with sf() as s:
        repo = SqlAlchemyResourceBindingRepository(s)
        await repo.create(
            workspace_id=ws, product_id=product_id, connector_account_id=conn_id, resource_id="a"
        )
        await repo.create(
            workspace_id=ws, product_id=product_id, connector_account_id=conn_id, resource_id="b"
        )
        await s.commit()

    async with sf() as s:
        repo = SqlAlchemyResourceBindingRepository(s)
        rows = await repo.list_for_product(workspace_id=ws, product_id=product_id)
        assert {r.resource_id for r in rows} == {"a", "b"}


async def test_update_changes_knobs(sf) -> None:
    ws = uuid.uuid4()
    product_id, conn_id = await _seed_parents(sf, workspace_id=ws)

    async with sf() as s:
        repo = SqlAlchemyResourceBindingRepository(s)
        row = await repo.create(
            workspace_id=ws,
            product_id=product_id,
            connector_account_id=conn_id,
            resource_id="r",
        )
        await s.commit()
        binding_id = row.id

    async with sf() as s:
        repo = SqlAlchemyResourceBindingRepository(s)
        row = await repo.get(workspace_id=ws, binding_id=binding_id)
        assert row is not None
        await repo.update(
            row,
            output_mode="direct",
            trigger={"enabled": True, "filters": {}},
            selection={"folder": "inbox"},
        )
        await s.commit()

    async with sf() as s:
        repo = SqlAlchemyResourceBindingRepository(s)
        got = await repo.get(workspace_id=ws, binding_id=binding_id)
        assert got is not None
        assert got.output_mode == "direct"
        assert got.trigger == {"enabled": True, "filters": {}}
        assert got.selection == {"folder": "inbox"}


async def test_delete(sf) -> None:
    ws = uuid.uuid4()
    product_id, conn_id = await _seed_parents(sf, workspace_id=ws)

    async with sf() as s:
        repo = SqlAlchemyResourceBindingRepository(s)
        row = await repo.create(
            workspace_id=ws, product_id=product_id, connector_account_id=conn_id, resource_id="r"
        )
        await s.commit()
        binding_id = row.id

    async with sf() as s:
        repo = SqlAlchemyResourceBindingRepository(s)
        deleted = await repo.delete(workspace_id=ws, binding_id=binding_id)
        await s.commit()
        assert deleted is True

    async with sf() as s:
        repo = SqlAlchemyResourceBindingRepository(s)
        assert await repo.get(workspace_id=ws, binding_id=binding_id) is None
        # Deleting again is a no-op False (already gone).
        assert await repo.delete(workspace_id=ws, binding_id=binding_id) is False


async def test_find_binding_resolves_connector_and_resource(sf) -> None:
    """The B10b lookup: (connector_account_id, resource_id) → the binding."""
    ws = uuid.uuid4()
    product_id, conn_id = await _seed_parents(sf, workspace_id=ws)

    async with sf() as s:
        repo = SqlAlchemyResourceBindingRepository(s)
        await repo.create(
            workspace_id=ws,
            product_id=product_id,
            connector_account_id=conn_id,
            resource_id="bsvibe/bsvibe-site#42",
            output_mode="direct",
        )
        await s.commit()

    async with sf() as s:
        repo = SqlAlchemyResourceBindingRepository(s)
        found = await repo.find_binding(
            connector_account_id=conn_id, resource_id="bsvibe/bsvibe-site#42"
        )
        assert found is not None
        assert found.product_id == product_id
        assert found.output_mode == "direct"
        # A miss returns None (not an error).
        assert await repo.find_binding(connector_account_id=conn_id, resource_id="nope") is None


async def test_workspace_isolation_get_and_list(sf) -> None:
    """A binding in workspace A is invisible to workspace B via get/list."""
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    product_a, conn_a = await _seed_parents(sf, workspace_id=ws_a)
    product_b, _conn_b = await _seed_parents(sf, workspace_id=ws_b)

    async with sf() as s:
        repo = SqlAlchemyResourceBindingRepository(s)
        row = await repo.create(
            workspace_id=ws_a,
            product_id=product_a,
            connector_account_id=conn_a,
            resource_id="secret",
        )
        await s.commit()
        binding_id = row.id

    async with sf() as s:
        repo = SqlAlchemyResourceBindingRepository(s)
        # B can't get A's binding.
        assert await repo.get(workspace_id=ws_b, binding_id=binding_id) is None
        # B's product list is empty.
        assert await repo.list_for_product(workspace_id=ws_b, product_id=product_b) == []
        # B can't delete A's binding.
        assert await repo.delete(workspace_id=ws_b, binding_id=binding_id) is False
        # A still sees it.
        assert await repo.get(workspace_id=ws_a, binding_id=binding_id) is not None
