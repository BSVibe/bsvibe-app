"""CLI: ``python -m backend.workflow.application.runtime.bootstrap_anchor_backfill``.

Retrofits ``concepts/active/<id>.md`` canonical anchors for products that
finished bootstrap on the pre-Lift-A-fix code path (where no promotion ran).
Pins target resolution, dry-run, idempotency, and the empty-match exit code.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.identity.workspaces_db import ProductRow, WorkspaceRow, WorkspacesBase
from backend.workflow.application.runtime.bootstrap_anchor_backfill import run_backfill

from .._support import db_engine

pytestmark = pytest.mark.asyncio

_REGION = "us-1"


@pytest_asyncio.fixture
async def session_factory():
    async with db_engine(WorkspacesBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


def _seed_observation(vault: Path, slug: str, tags: list[str]) -> None:
    path = vault / "garden" / "seedling" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tag_yaml = "\n".join(f"  - {t}" for t in tags)
    path.write_text(
        f"---\ntags:\n{tag_yaml}\n---\n# {slug}\n",
        encoding="utf-8",
    )


def _seed_workspace_vault(vault_root: Path, workspace_id: uuid.UUID) -> Path:
    """Create a per-workspace vault with two recurring tags."""
    ws_vault = vault_root / _REGION / str(workspace_id)
    ws_vault.mkdir(parents=True)
    _seed_observation(ws_vault, "obs-1", ["settle", "verified-run", "alpha"])
    _seed_observation(ws_vault, "obs-2", ["settle", "verified-run", "alpha"])
    _seed_observation(ws_vault, "obs-3", ["settle", "verified-run", "beta"])
    _seed_observation(ws_vault, "obs-4", ["settle", "verified-run", "beta"])
    return ws_vault


async def _seed_product(
    session_factory: async_sessionmaker,
    *,
    workspace_id: uuid.UUID,
    product_id: uuid.UUID,
    slug: str,
    status: str = "complete",
) -> None:
    async with session_factory() as s:
        existing = await s.get(WorkspaceRow, workspace_id)
        if existing is None:
            s.add(WorkspaceRow(id=workspace_id, name="t", region=_REGION, safe_mode=False))
            await s.flush()
        s.add(
            ProductRow(
                id=product_id,
                workspace_id=workspace_id,
                name=slug,
                slug=slug,
                repo_url="https://x/y",
                bootstrap_status=status,
            )
        )
        await s.commit()


async def test_backfill_by_product_slug_creates_anchors(session_factory, tmp_path: Path):
    """``--product-slug X`` retrofits exactly that product's workspace vault."""
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    await _seed_product(
        session_factory,
        workspace_id=workspace_id,
        product_id=product_id,
        slug="alpha-product",
    )
    ws_vault = _seed_workspace_vault(tmp_path, workspace_id)

    processed = await run_backfill(
        session_factory=session_factory,
        product_slug="alpha-product",
        workspace_id=None,
        vault_root=tmp_path,
        dry_run=False,
    )

    assert processed == 1
    assert (ws_vault / "concepts" / "active" / "alpha.md").exists()
    assert (ws_vault / "concepts" / "active" / "beta.md").exists()


async def test_backfill_dry_run_does_not_mutate_vault(session_factory, tmp_path: Path):
    """``--dry-run`` resolves targets but leaves the vault untouched."""
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    await _seed_product(
        session_factory,
        workspace_id=workspace_id,
        product_id=product_id,
        slug="dry-run-product",
    )
    ws_vault = _seed_workspace_vault(tmp_path, workspace_id)

    processed = await run_backfill(
        session_factory=session_factory,
        product_slug="dry-run-product",
        workspace_id=None,
        vault_root=tmp_path,
        dry_run=True,
    )

    assert processed == 1
    assert not (ws_vault / "concepts" / "active" / "alpha.md").exists()


async def test_backfill_returns_zero_when_no_match(session_factory, tmp_path: Path):
    """Unknown slug → returns 0 so the CLI exits non-zero (operator typo)."""
    processed = await run_backfill(
        session_factory=session_factory,
        product_slug="does-not-exist",
        workspace_id=None,
        vault_root=tmp_path,
        dry_run=False,
    )
    assert processed == 0


async def test_backfill_skips_incomplete_bootstrap_products(session_factory, tmp_path: Path):
    """Products with ``bootstrap_status != 'complete'`` are not eligible."""
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    await _seed_product(
        session_factory,
        workspace_id=workspace_id,
        product_id=product_id,
        slug="failed-product",
        status="failed:clone",
    )
    _seed_workspace_vault(tmp_path, workspace_id)

    processed = await run_backfill(
        session_factory=session_factory,
        product_slug="failed-product",
        workspace_id=None,
        vault_root=tmp_path,
        dry_run=False,
    )

    assert processed == 0


async def test_backfill_idempotent_on_second_run(session_factory, tmp_path: Path):
    """A second pass over a vault that already has anchors is a no-op."""
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    await _seed_product(
        session_factory,
        workspace_id=workspace_id,
        product_id=product_id,
        slug="idem-product",
    )
    ws_vault = _seed_workspace_vault(tmp_path, workspace_id)

    await run_backfill(
        session_factory=session_factory,
        product_slug="idem-product",
        workspace_id=None,
        vault_root=tmp_path,
        dry_run=False,
    )
    first_listing = sorted(p.name for p in (ws_vault / "concepts" / "active").iterdir())

    await run_backfill(
        session_factory=session_factory,
        product_slug="idem-product",
        workspace_id=None,
        vault_root=tmp_path,
        dry_run=False,
    )
    second_listing = sorted(p.name for p in (ws_vault / "concepts" / "active").iterdir())

    assert first_listing == second_listing
    assert "alpha.md" in first_listing
    assert "beta.md" in first_listing


async def test_backfill_by_workspace_id_processes_every_product(session_factory, tmp_path: Path):
    """``--workspace-id X`` retrofits every complete-bootstrap product in workspace."""
    workspace_id = uuid.uuid4()
    await _seed_product(
        session_factory,
        workspace_id=workspace_id,
        product_id=uuid.uuid4(),
        slug="ws-product-a",
    )
    await _seed_product(
        session_factory,
        workspace_id=workspace_id,
        product_id=uuid.uuid4(),
        slug="ws-product-b",
    )
    _seed_workspace_vault(tmp_path, workspace_id)

    processed = await run_backfill(
        session_factory=session_factory,
        product_slug=None,
        workspace_id=workspace_id,
        vault_root=tmp_path,
        dry_run=False,
    )
    # Both products map to the SAME workspace vault, but the retrofit is
    # workspace-scoped so it runs once per product row matching the predicate.
    assert processed == 2
