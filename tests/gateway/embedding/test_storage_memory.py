"""InMemoryVectorBackend — Python cosine + stale-model filtering."""

from __future__ import annotations

import uuid

import pytest

from backend.gateway.embedding.storage.memory import (
    InMemoryVectorBackend,
    VectorEntry,
)


@pytest.fixture
def backend() -> InMemoryVectorBackend:
    return InMemoryVectorBackend()


WS = uuid.uuid4()
ACCT = uuid.uuid4()


class TestSearch:
    async def test_returns_top_k_by_descending_similarity(self, backend):
        ids = [uuid.uuid4() for _ in range(3)]
        await backend.upsert(
            [
                VectorEntry(
                    id=ids[0],
                    workspace_id=WS,
                    account_id=ACCT,
                    intent_id=uuid.uuid4(),
                    embedding=[1.0, 0.0],
                    embedding_model="m",
                ),
                VectorEntry(
                    id=ids[1],
                    workspace_id=WS,
                    account_id=ACCT,
                    intent_id=uuid.uuid4(),
                    embedding=[0.7, 0.7],
                    embedding_model="m",
                ),
                VectorEntry(
                    id=ids[2],
                    workspace_id=WS,
                    account_id=ACCT,
                    intent_id=uuid.uuid4(),
                    embedding=[0.0, 1.0],
                    embedding_model="m",
                ),
            ]
        )
        results = await backend.search(
            query=[1.0, 0.0],
            workspace_id=WS,
            account_id=ACCT,
            embedding_model="m",
            limit=3,
        )
        # First hit must be exact match (id 0); last hit must be orthogonal (id 2).
        assert results[0].entry.id == ids[0]
        assert results[0].similarity > 0.99
        assert results[-1].entry.id == ids[2]
        assert results[-1].similarity < 0.01

    async def test_filters_by_account(self, backend):
        other = uuid.uuid4()
        await backend.upsert(
            [
                VectorEntry(
                    id=uuid.uuid4(),
                    workspace_id=WS,
                    account_id=ACCT,
                    intent_id=uuid.uuid4(),
                    embedding=[1.0, 0.0],
                    embedding_model="m",
                ),
                VectorEntry(
                    id=uuid.uuid4(),
                    workspace_id=WS,
                    account_id=other,
                    intent_id=uuid.uuid4(),
                    embedding=[1.0, 0.0],
                    embedding_model="m",
                ),
            ]
        )
        results = await backend.search(
            query=[1.0, 0.0],
            workspace_id=WS,
            account_id=ACCT,
            embedding_model="m",
            limit=10,
        )
        assert len(results) == 1
        assert results[0].entry.account_id == ACCT

    async def test_filters_stale_model(self, backend):
        await backend.upsert(
            [
                VectorEntry(
                    id=uuid.uuid4(),
                    workspace_id=WS,
                    account_id=ACCT,
                    intent_id=uuid.uuid4(),
                    embedding=[1.0, 0.0],
                    embedding_model="old",
                ),
                VectorEntry(
                    id=uuid.uuid4(),
                    workspace_id=WS,
                    account_id=ACCT,
                    intent_id=uuid.uuid4(),
                    embedding=[0.9, 0.1],
                    embedding_model="new",
                ),
            ]
        )
        results = await backend.search(
            query=[1.0, 0.0],
            workspace_id=WS,
            account_id=ACCT,
            embedding_model="new",
            limit=10,
        )
        assert len(results) == 1
        assert results[0].entry.embedding_model == "new"

    async def test_empty_returns_empty(self, backend):
        results = await backend.search(
            query=[1.0, 0.0],
            workspace_id=WS,
            account_id=ACCT,
            embedding_model="m",
            limit=10,
        )
        assert results == []

    async def test_zero_norm_query_returns_zero_similarity(self, backend):
        await backend.upsert(
            [
                VectorEntry(
                    id=uuid.uuid4(),
                    workspace_id=WS,
                    account_id=ACCT,
                    intent_id=uuid.uuid4(),
                    embedding=[0.0, 0.0],
                    embedding_model="m",
                ),
            ]
        )
        results = await backend.search(
            query=[1.0, 0.0],
            workspace_id=WS,
            account_id=ACCT,
            embedding_model="m",
            limit=10,
        )
        assert results[0].similarity == 0.0


class TestRemove:
    async def test_remove_by_intent(self, backend):
        intent_a = uuid.uuid4()
        intent_b = uuid.uuid4()
        await backend.upsert(
            [
                VectorEntry(
                    id=uuid.uuid4(),
                    workspace_id=WS,
                    account_id=ACCT,
                    intent_id=intent_a,
                    embedding=[1.0],
                    embedding_model="m",
                ),
                VectorEntry(
                    id=uuid.uuid4(),
                    workspace_id=WS,
                    account_id=ACCT,
                    intent_id=intent_b,
                    embedding=[1.0],
                    embedding_model="m",
                ),
            ]
        )
        await backend.remove_intent(intent_a)
        results = await backend.search(
            query=[1.0],
            workspace_id=WS,
            account_id=ACCT,
            embedding_model="m",
            limit=10,
        )
        assert len(results) == 1
        assert results[0].entry.intent_id == intent_b
