"""#929 (B), stage 2 PR 2c — originals, ingest indexes and the code graph go through storage.

Same contract as ``test_vault_io.py``: each runs unchanged on the files it always used and on
``vault_documents`` rows. See docs/design/stateless-workers.md §5.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import networkx as nx
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.knowledge.graph.vault import Vault
from backend.knowledge.infrastructure.document_store import PgDocumentStorage

from .._support import db_engine


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[Any]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture(params=["files", "rows"])
async def vault(request, tmp_path: Path, sf) -> Vault:
    root = tmp_path / "vault" / "us-1" / "ws"
    if request.param == "files":
        return Vault(root)
    return Vault(root, storage=PgDocumentStorage(sf, workspace_id=uuid.uuid4(), tree="vault"))


# --- create-if-absent ---------------------------------------------------------------------------


async def test_create_writes_once_and_the_first_one_wins(vault) -> None:
    path = vault.resolve_path("seeds/request/k.md")

    assert await vault.create(path, "first") is True
    assert await vault.create(path, "second") is False
    assert await vault.read(path) == "first"


async def test_an_original_is_recorded_once_on_either_store(vault) -> None:
    from backend.knowledge.originals import record_original

    written = await record_original(
        vault=vault, kind="request", key="r-1", title="T", content="what the founder said"
    )
    again = await record_original(
        vault=vault, kind="request", key="r-1", title="T", content="something else"
    )

    assert written == vault.resolve_path("seeds/request/r-1.md") and again is None
    assert "what the founder said" in await vault.read(written)


# --- directories --------------------------------------------------------------------------------


async def test_child_directories_are_listed_from_the_notes(vault) -> None:
    for rel in ("seeds/request/a.md", "seeds/feedback/b.md", "seeds/top.md", "ideas/x.md"):
        await vault.write(rel, rel)

    assert await vault.list_dirs("seeds") == ["feedback", "request"]
    assert await vault.list_dirs("") == ["ideas", "seeds"]
    assert await vault.list_dirs("nowhere") == []


async def test_the_file_index_reads_and_writes_through_the_store(vault) -> None:
    from backend.knowledge.ingest.file_index_reader import FileIndexReader

    await vault.write("seeds/request/r.md", "---\ntitle: Ask\ntype: original\n---\nbody\n")
    await vault.write("garden/seedling/n.md", "---\ntitle: Note\n---\nbody\n")
    await vault.write("ideas/legacy.md", "---\ntitle: Legacy\n---\nbody\n")
    reader = FileIndexReader(vault)

    await reader.write_catalog()
    await reader.rebuild_all()

    catalog = await vault.read("index.md")
    assert "[[Ask]]" in catalog and "[[Note]]" in catalog and "[[Legacy]]" in catalog
    assert await vault.exists(vault.root / "_index" / "overview.md")


# --- the code graph -----------------------------------------------------------------------------


def _graph() -> nx.DiGraph:
    """Three nodes in one community — the smallest one that earns a label."""
    g: nx.DiGraph = nx.DiGraph()
    for nid, file in (("a.f", "pkg/a.py"), ("b.g", "pkg/b.py"), ("c.h", "pkg/c.py")):
        g.add_node(nid, id=nid, kind="function", file=file, path=file, community_id=0)
    g.add_edge("a.f", "b.g", kind="calls")
    g.add_edge("b.g", "c.h", kind="calls")
    return g


async def test_the_code_graph_round_trips_through_the_store(vault) -> None:
    from backend.knowledge.code_graph.pipeline import (
        load_code_graph,
        load_community_labels,
        save_code_graph,
    )

    assert await load_code_graph(vault) is None, "no graph before the first bootstrap"

    labeled = await save_code_graph(vault, _graph())
    loaded = await load_code_graph(vault)

    assert loaded is not None
    assert set(loaded.nodes) == {"a.f", "b.g", "c.h"} and loaded.has_edge("a.f", "b.g")
    assert labeled == 1
    assert list(await load_community_labels(vault)) == [0], "the sidecar is there too"
    assert await vault.exists("code_graph/graph.json")
