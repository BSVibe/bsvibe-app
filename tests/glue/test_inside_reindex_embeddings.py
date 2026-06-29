"""``POST /api/v1/inside/reindex-embeddings`` — the embedding backfill trigger.

Drives the endpoint end-to-end with the DI builders overridden to a fixture
vault + an in-memory vector backend + a fake embedder, so the HTTP wiring is
exercised without a live Postgres / embedding provider.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.api.v1.inside.embeddings import (
    build_inside_embedder,
    build_inside_vault,
    build_inside_vector_backend,
)
from backend.knowledge.graph.vault import Vault
from backend.knowledge.retrieval.storage.memory import InMemoryNoteVectorBackend

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


class _FakeEmbedder:
    @property
    def enabled(self) -> bool:
        return True

    @property
    def model(self) -> str | None:
        return "fake-model"

    async def embed(self, text: str) -> list[float]:
        return [float(len(text) % 5), 1.0, 0.0]


async def test_reindex_embeddings_backfills_missing_knowledge_notes(tmp_path) -> None:
    vault = Vault(tmp_path)
    for rel, body in [
        ("garden/seedling/a.md", "Alpha principle"),
        ("concepts/active/c.md", "Gamma synthesis"),
    ]:
        p = vault.resolve_path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\ntitle: T\n---\n\n# T\n\n{body}\n")
    store = InMemoryNoteVectorBackend()

    app = create_app()
    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = lambda: uuid.uuid4()
    app.dependency_overrides[build_inside_vault] = lambda: vault
    app.dependency_overrides[build_inside_embedder] = lambda: _FakeEmbedder()
    app.dependency_overrides[build_inside_vector_backend] = lambda: store

    async with db_engine() as (engine, _is_pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)

        async def _session():
            async with sf() as s:
                yield s

        app.dependency_overrides[get_db_session] = _session
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/api/v1/inside/reindex-embeddings")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"scanned": 2, "embedded": 2, "already": 0, "disabled": False}
    assert await store.existing_paths() == {
        "garden/seedling/a.md",
        "concepts/active/c.md",
    }
