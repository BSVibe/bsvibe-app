"""#929 (B), stage 2 PR 5 — moving the vault and skills into rows, and back out.

``python -m backend.knowledge.store import`` copies every workspace's vault
(``<knowledge_vault_root>/<region>/<ws>/``) and skills (``<skills_root>/<ws>/``) into
``vault_documents``, byte for byte, at the same relative paths. It is safe to run again — an
unchanged document is not rewritten — and ``--dry-run`` writes nothing. ``export <dir>`` writes
the rows back out as the same tree, so the Markdown is always one command away.
docs/design/stateless-workers.md §5.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.config import get_settings
from backend.knowledge import store
from backend.knowledge.infrastructure.document_store import PgDocumentStorage

from .._support import db_engine


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[Any]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    s = get_settings()
    monkeypatch.setattr(s, "knowledge_vault_root", str(tmp_path / "vault"), raising=False)
    monkeypatch.setattr(s, "skills_root", str(tmp_path / "skills"), raising=False)
    ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
    vault_base = tmp_path / "vault" / s.knowledge_default_region
    files = {
        (ws_a, "vault", "garden/seedling/a.md"): "---\ntitle: A\n---\n한국어 본문\n",
        (ws_a, "vault", "code_graph/graph.json"): '{"nodes": [], "edges": []}',
        (ws_a, "vault", ".bsage/ontology.yaml"): "schema_version: 1\n",
        (ws_b, "vault", "seeds/request/r.md"): "original\n",
        (ws_a, "skills", "review.md"): "---\nname: review\nversion: 1\ndescription: d\n---\n",
    }
    for (ws, tree, rel), content in files.items():
        root = vault_base / str(ws) if tree == "vault" else tmp_path / "skills" / str(ws)
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content, encoding="utf-8")
    # Not a workspace: ignored, never imported under a made-up id.
    (vault_base / "not-a-uuid").mkdir(parents=True)
    (vault_base / "not-a-uuid" / "x.md").write_text("x")
    return {"files": files, "root": tmp_path}


async def _rows(sf, ws: uuid.UUID, tree: str) -> dict[str, str]:
    storage = PgDocumentStorage(sf, workspace_id=ws, tree=tree)  # type: ignore[arg-type]
    return await storage.read_many(await storage.list_files("", "*"))


async def test_import_copies_every_document_exactly(sf, disk) -> None:
    report = await store.import_from_disk(sf)

    assert report.written == len(disk["files"]) and report.unchanged == 0
    for (ws, tree, rel), content in disk["files"].items():
        assert (await _rows(sf, ws, tree))[rel] == content


async def test_a_second_import_rewrites_nothing(sf, disk) -> None:
    await store.import_from_disk(sf)

    again = await store.import_from_disk(sf)

    assert (again.written, again.unchanged) == (0, len(disk["files"]))


async def test_a_dry_run_writes_nothing(sf, disk) -> None:
    report = await store.import_from_disk(sf, dry_run=True)

    assert report.written == len(disk["files"])
    for ws, tree, _ in disk["files"]:
        assert await _rows(sf, ws, tree) == {}


async def test_export_writes_the_same_tree_back_out(sf, disk, tmp_path: Path) -> None:
    await store.import_from_disk(sf)
    out = tmp_path / "export"

    count = await store.export_to_disk(sf, out)

    assert count == len(disk["files"])
    vault_out = out / "vault" / get_settings().knowledge_default_region
    for (ws, tree, rel), content in disk["files"].items():
        base = vault_out / str(ws) if tree == "vault" else out / "skills" / str(ws)
        assert (base / rel).read_text(encoding="utf-8") == content


def test_the_command_line_names_both_directions() -> None:
    assert store._parse_args(["import", "--dry-run"]).command == "import"
    assert store._parse_args(["export", "/tmp/x"]).out == Path("/tmp/x")
