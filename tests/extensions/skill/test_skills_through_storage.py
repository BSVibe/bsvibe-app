"""#929 (B), stage 2 PR 2d — skills live wherever ``knowledge_store_backend`` says.

The API writes skills and the worker reads them, so on more than one machine they have to be
in the shared store. One module (``backend.extensions.skill.store``) owns them: loading,
creating (atomically — a second create with the same slug is refused, not an overwrite) and
updating, for the REST surface, the MCP tools and the agent worker alike.
docs/design/stateless-workers.md §5.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.config import get_settings
from backend.extensions.skill import SkillLoader
from backend.extensions.skill.store import (
    SkillConflict,
    SkillNotFound,
    create_workspace_skill,
    update_workspace_skill,
    workspace_skill_loader,
)
from backend.knowledge import vault_store
from backend.knowledge.infrastructure.document_store import PgDocumentStorage

from ..._support import db_engine


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[Any]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(params=["filesystem", "postgres"])
def store(request, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sf) -> str:
    s = get_settings()
    monkeypatch.setattr(s, "knowledge_store_backend", request.param, raising=False)
    monkeypatch.setattr(s, "skills_root", str(tmp_path / "skills"), raising=False)
    monkeypatch.setattr(vault_store, "_session_factory", lambda: sf)
    return request.param


async def test_a_created_skill_loads_back(store) -> None:
    ws = uuid.uuid4()

    meta = await create_workspace_skill(
        ws, name="Code Review", summary="Review a diff", system_prompt="You review code."
    )
    loader = await workspace_skill_loader(ws)

    assert meta.name == "code-review"
    assert loader.get("code-review").description == "Review a diff"
    assert loader.get("code-review").system_prompt == "You review code."


async def test_a_second_create_with_the_same_slug_is_refused(store) -> None:
    ws = uuid.uuid4()
    await create_workspace_skill(ws, name="review", summary="first", system_prompt="one")

    with pytest.raises(SkillConflict):
        await create_workspace_skill(ws, name="Review", summary="second", system_prompt="two")

    assert (await workspace_skill_loader(ws)).get("review").description == "first"


async def test_an_update_rewrites_body_and_summary_keeping_the_version(store) -> None:
    ws = uuid.uuid4()
    await vault_store.workspace_skills_storage(ws).write(
        "review.md", "---\nname: review\nversion: 2.3.0\ndescription: old\n---\nold body\n"
    )

    meta = await update_workspace_skill(ws, name="review", summary="new", system_prompt="new body")

    assert (meta.description, meta.system_prompt, meta.version) == ("new", "new body", "2.3.0")
    with pytest.raises(SkillNotFound):
        await update_workspace_skill(ws, name="missing", summary="x", system_prompt="y")


async def test_skills_are_per_workspace(store) -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    await create_workspace_skill(a, name="mine", summary="s", system_prompt="p")

    assert "mine" in (await workspace_skill_loader(a)).registry
    assert (await workspace_skill_loader(b)).registry == {}


async def test_the_loader_reads_top_level_manifests_and_skips_broken_ones(sf) -> None:
    storage = PgDocumentStorage(sf, workspace_id=uuid.uuid4(), tree="skills")
    await storage.write("ok.md", "---\nname: ok\nversion: 1.0.0\ndescription: d\n---\nbody\n")
    await storage.write("broken.md", "no frontmatter at all\n")
    await storage.write("nested/deep.md", "---\nname: deep\nversion: 1\ndescription: d\n---\n")

    loader = SkillLoader(storage)
    await loader.load()

    assert list(loader.registry) == ["ok"]


def test_rest_and_mcp_share_the_one_skill_store() -> None:
    from backend.api.v1 import skills as rest
    from backend.mcp.tools import skills_tools as mcp

    for module in (rest, mcp):
        src = inspect.getsource(module)
        assert "create_workspace_skill(" in src and "update_workspace_skill(" in src, module
        assert "write_text(" not in src and "skills_root" not in src, module


async def test_the_worker_takes_an_async_skill_loader() -> None:
    """Production resolves skills from the store (async); a sync stub still works."""
    from backend.workflow.infrastructure.workers.agent_worker import _resolve_skill_loader

    loader = SkillLoader(Path("/nonexistent"))

    async def _async(ws: uuid.UUID) -> SkillLoader:
        return loader

    assert await _resolve_skill_loader(_async, uuid.uuid4()) is loader
    assert await _resolve_skill_loader(lambda ws: loader, uuid.uuid4()) is loader
