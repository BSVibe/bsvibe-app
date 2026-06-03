"""Lift A-fix runtime — bootstrap calls anchor registration after ingest.

Validates that ``run_product_bootstrap_job`` invokes the post-ingest anchor
registration step against the workspace vault, so the
``garden/`` notes that ingest just wrote get promoted to
``concepts/active/<id>.md`` anchors that the PWA Knowledge graph view reads.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.identity.workspaces_db import ProductRow, WorkspaceRow, WorkspacesBase
from backend.workflow.application.runtime.product_bootstrap_runtime import (
    run_product_bootstrap_job,
)

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


async def test_bootstrap_job_registers_anchors_after_successful_ingest(
    session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ingest succeeds, the runtime must call anchor registration on the vault.

    Approach: stub ``build_bootstrap_knowledge`` to return a fake Knowledge whose
    ``ingest`` *simulates* what real ingest does — writes a few garden notes with
    recurring tags into the per-workspace vault. After the job, ``concepts/active/``
    must contain anchors for those recurring tags.
    """
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    async with session_factory() as s:
        s.add(WorkspaceRow(id=workspace_id, name="t", region=_REGION, safe_mode=False))
        await s.flush()
        s.add(
            ProductRow(
                id=product_id,
                workspace_id=workspace_id,
                name="p",
                slug="p",
                repo_url="https://x/y",
            )
        )
        await s.commit()

    # Point both product workspace root + knowledge vault root at tmp_path so
    # the runtime's clone + ingest stay inside the sandbox.
    from backend.config import get_settings

    settings = get_settings()
    product_root = tmp_path / "product_ws"
    product_root.mkdir()
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    object.__setattr__(settings, "product_workspace_root", str(product_root))
    object.__setattr__(settings, "knowledge_vault_root", str(vault_root))

    # Fake git clone: do nothing successful.
    fake_git = MagicMock()
    fake_git.clone = AsyncMock()

    # Stub the knowledge facade — its ingest writes seedling notes with
    # recurring tags into the workspace vault, mimicking what real ingest does.
    workspace_vault = vault_root / _REGION / str(workspace_id)

    class _StubKnowledge:
        async def ingest(self, request):
            from backend.knowledge.facade import IngestResult

            _seed_observation(
                workspace_vault, "stub-obs-1", ["settle", "verified-run", "stub-concept"]
            )
            _seed_observation(
                workspace_vault, "stub-obs-2", ["settle", "verified-run", "stub-concept"]
            )
            return IngestResult(
                proposals_count=0,
                notes_count=2,
                run_id=uuid.uuid4(),
            )

        async def retrieve_canon(self, query):
            from backend.knowledge.facade import CanonRetrievalResult

            return CanonRetrievalResult(notes=[])

        async def settle(self, *, workspace_id, region):
            return 0

    monkeypatch.setattr(
        "backend.workflow.application.runtime.product_bootstrap_runtime.build_bootstrap_knowledge",
        lambda **_kw: _StubKnowledge(),
    )

    await run_product_bootstrap_job(
        product_id=product_id,
        workspace_id=workspace_id,
        repo_url="https://x/y",
        session_factory=session_factory,
        git_ops=fake_git,
    )

    async with session_factory() as s:
        row = await s.get(ProductRow, product_id)
        assert row is not None
        assert row.bootstrap_status == "complete", row.bootstrap_error

    # The recurring tag got promoted to a canonical anchor.
    concept_file = workspace_vault / "concepts" / "active" / "stub-concept.md"
    assert concept_file.exists(), (
        f"expected concepts/active/stub-concept.md after bootstrap, "
        f"vault tree: {sorted(p.relative_to(workspace_vault) for p in workspace_vault.rglob('*.md'))}"
    )
