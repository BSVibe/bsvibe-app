"""GDPR L1 — workspaces.legal_basis column.

Adds a discrete legal-basis marker to every Workspace (Art. 6).
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import backend.connectors.db  # noqa: F401 — FK target for resource_bindings
from backend.identity.workspaces_db import WorkspaceRow, WorkspacesBase
from tests._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session_factory():
    async with db_engine(WorkspacesBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def test_legal_basis_defaults_to_contract(session_factory) -> None:
    """A new row with no legal_basis provided defaults to ``'contract'``."""
    workspace_id = uuid.uuid4()
    async with session_factory() as s:
        s.add(WorkspaceRow(id=workspace_id, name="t", region="us-1", safe_mode=True))
        await s.commit()
    async with session_factory() as s:
        row = (
            await s.execute(select(WorkspaceRow).where(WorkspaceRow.id == workspace_id))
        ).scalar_one()
        assert row.legal_basis == "contract"


async def test_legal_basis_persists_consent(session_factory) -> None:
    """Explicitly setting ``'consent'`` round-trips."""
    workspace_id = uuid.uuid4()
    async with session_factory() as s:
        s.add(
            WorkspaceRow(
                id=workspace_id,
                name="t",
                region="us-1",
                safe_mode=True,
                legal_basis="consent",
            )
        )
        await s.commit()
    async with session_factory() as s:
        row = (
            await s.execute(select(WorkspaceRow).where(WorkspaceRow.id == workspace_id))
        ).scalar_one()
        assert row.legal_basis == "consent"


@pytest.mark.parametrize("invalid", ["legitimate_interest", "", "CONTRACT"])
async def test_legal_basis_literal_validation_rejects_garbage(invalid: str) -> None:
    """The validator on the column must reject values outside the Literal."""
    from backend.identity.workspaces_db import validate_legal_basis

    assert validate_legal_basis("contract") == "contract"
    assert validate_legal_basis("consent") == "consent"
    with pytest.raises(ValueError):
        validate_legal_basis(invalid)
