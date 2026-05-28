"""G3 — Note vector backend (pgvector for prod, in-memory for tests).

Mirrors the gateway embedding storage policy (``VectorSearchBackend`` →
``PgVectorBackend`` / ``InMemoryVectorBackend``): the note semantic-search store
is a ``NoteVectorBackend`` Protocol with a Postgres+pgvector prod impl and an
in-memory test impl. These tests pin the in-memory backend's contract (the one
the broad SQLite suite uses) + the cross-dialect ORM round-trip of the
``EmbeddingVector`` column; the real pgvector ``<=>`` path is covered by the
fresh-PG migration test + a BSVIBE_DATABASE_URL-gated smoke test.
"""

from __future__ import annotations

import uuid

import pytest

from backend.knowledge.retrieval.storage.backend import NoteVectorBackend
from backend.knowledge.retrieval.storage.memory import InMemoryNoteVectorBackend

pytestmark = pytest.mark.asyncio


async def test_in_memory_backend_satisfies_protocol() -> None:
    assert isinstance(InMemoryNoteVectorBackend(), NoteVectorBackend)


async def test_store_and_search_orders_by_similarity() -> None:
    backend = InMemoryNoteVectorBackend()
    await backend.store("garden/a.md", [1.0, 0.0, 0.0])
    await backend.store("garden/b.md", [0.0, 1.0, 0.0])
    await backend.store("garden/c.md", [0.9, 0.1, 0.0])

    results = await backend.search([1.0, 0.0, 0.0], top_k=2)
    paths = [p for p, _ in results]
    # a is an exact match; c is close; b is orthogonal — a first, then c.
    assert paths == ["garden/a.md", "garden/c.md"]
    # similarity is a real cosine in [−1, 1], a's is ~1.0.
    assert results[0][1] == pytest.approx(1.0, abs=1e-6)


async def test_store_is_upsert() -> None:
    backend = InMemoryNoteVectorBackend()
    await backend.store("garden/a.md", [1.0, 0.0])
    await backend.store("garden/a.md", [0.0, 1.0])  # overwrite
    results = await backend.search([0.0, 1.0], top_k=5)
    assert len(results) == 1
    assert results[0][0] == "garden/a.md"
    assert results[0][1] == pytest.approx(1.0, abs=1e-6)


async def test_remove() -> None:
    backend = InMemoryNoteVectorBackend()
    await backend.store("garden/a.md", [1.0, 0.0])
    await backend.store("garden/b.md", [0.0, 1.0])
    await backend.remove("garden/a.md")
    results = await backend.search([1.0, 0.0], top_k=5)
    assert [p for p, _ in results] == ["garden/b.md"]


async def test_search_empty_backend_returns_empty() -> None:
    backend = InMemoryNoteVectorBackend()
    assert await backend.search([1.0, 0.0], top_k=5) == []


async def test_two_instances_are_isolated() -> None:
    """Each workspace gets its own in-memory backend instance (mirrors the
    per-vault VectorStore), so one workspace's notes never leak into another."""
    a = InMemoryNoteVectorBackend()
    b = InMemoryNoteVectorBackend()
    await a.store("garden/secret.md", [1.0, 0.0])
    assert await b.search([1.0, 0.0], top_k=5) == []


async def test_embedding_row_round_trips_on_sqlite() -> None:
    """The ``NoteEmbeddingRow`` ``EmbeddingVector`` column serializes a
    list[float] to a SQLite BLOB and back (the dialect path the broad test
    suite exercises; prod uses the pgvector column on the same model)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.knowledge.retrieval.db import NoteEmbeddingRow, RetrievalBase
    from tests._support import db_engine

    async with db_engine(RetrievalBase) as (engine, _is_pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws = uuid.uuid4()
        async with sf() as s:
            s.add(
                NoteEmbeddingRow(
                    workspace_id=ws,
                    note_path="garden/a.md",
                    embedding=[0.1, 0.2, 0.3],
                    embedding_model="test-model",
                    dimension=3,
                )
            )
            await s.commit()
        async with sf() as s:
            row = await s.get(NoteEmbeddingRow, {"workspace_id": ws, "note_path": "garden/a.md"})
            assert row is not None
            assert row.embedding == pytest.approx([0.1, 0.2, 0.3], abs=1e-6)
            assert row.embedding_model == "test-model"
