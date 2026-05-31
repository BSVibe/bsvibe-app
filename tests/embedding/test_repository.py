"""IntentRepository — definitions + examples CRUD, account-scoped."""

from __future__ import annotations

import uuid

import pytest

from backend.embedding.repository import (
    EmbeddingSettingsRepository,
    IntentDuplicateError,
    IntentRepository,
)
from backend.embedding.settings import EmbeddingSettings


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def account_id() -> uuid.UUID:
    return uuid.uuid4()


class TestIntentDefinitions:
    async def test_create_and_list(self, session, workspace_id, account_id):
        repo = IntentRepository(session)
        await repo.create_intent(
            workspace_id=workspace_id,
            account_id=account_id,
            name="support",
            description="customer help",
            threshold=0.7,
        )
        intents = await repo.list_intents(workspace_id=workspace_id, account_id=account_id)
        assert [i.name for i in intents] == ["support"]
        assert intents[0].threshold == 0.7

    async def test_unique_name_per_account(self, session, workspace_id, account_id):
        repo = IntentRepository(session)
        await repo.create_intent(workspace_id=workspace_id, account_id=account_id, name="dup")
        with pytest.raises(IntentDuplicateError):
            await repo.create_intent(workspace_id=workspace_id, account_id=account_id, name="dup")

    async def test_account_isolation(self, session, workspace_id):
        other = uuid.uuid4()
        own = uuid.uuid4()
        repo = IntentRepository(session)
        await repo.create_intent(workspace_id=workspace_id, account_id=own, name="r")
        # Same name allowed in different account.
        await repo.create_intent(workspace_id=workspace_id, account_id=other, name="r")
        own_intents = await repo.list_intents(workspace_id=workspace_id, account_id=own)
        assert [i.account_id for i in own_intents] == [own]


class TestIntentExamples:
    async def test_add_example_with_embedding(self, session, workspace_id, account_id):
        repo = IntentRepository(session)
        intent = await repo.create_intent(
            workspace_id=workspace_id, account_id=account_id, name="r"
        )
        ex = await repo.add_example(
            intent_id=intent.id,
            workspace_id=workspace_id,
            account_id=account_id,
            text="hi",
            embedding=[0.1, 0.2, 0.3],
            embedding_model="m",
        )
        assert ex.text == "hi"
        assert ex.embedding_model == "m"
        # Round-trip restored values within float32 precision.
        assert ex.embedding is not None
        for a, b in zip(ex.embedding, [0.1, 0.2, 0.3], strict=True):
            assert abs(a - b) < 1e-5
        assert ex.dimension == 3

    async def test_examples_needing_reembedding(self, session, workspace_id, account_id):
        repo = IntentRepository(session)
        intent = await repo.create_intent(
            workspace_id=workspace_id, account_id=account_id, name="r"
        )
        # Mix: stale (different model), missing (None), fresh.
        await repo.add_example(
            intent_id=intent.id,
            workspace_id=workspace_id,
            account_id=account_id,
            text="old",
            embedding=[1.0],
            embedding_model="old",
        )
        await repo.add_example(
            intent_id=intent.id,
            workspace_id=workspace_id,
            account_id=account_id,
            text="missing",
            embedding=None,
            embedding_model=None,
        )
        await repo.add_example(
            intent_id=intent.id,
            workspace_id=workspace_id,
            account_id=account_id,
            text="fresh",
            embedding=[1.0],
            embedding_model="new",
        )
        stale = await repo.list_examples_needing_reembedding(
            workspace_id=workspace_id,
            account_id=account_id,
            active_model="new",
        )
        # 2 need re-embedding (old + missing); 1 fresh.
        assert sorted(e.text for e in stale) == ["missing", "old"]


class TestEmbeddingSettingsRepo:
    async def test_upsert_and_get(self, session, workspace_id, account_id):
        repo = EmbeddingSettingsRepository(session)
        row = await repo.upsert(
            workspace_id=workspace_id,
            account_id=account_id,
            settings=EmbeddingSettings(model="ollama/nomic-embed-text"),
        )
        assert row.config["embedding"]["model"] == "ollama/nomic-embed-text"

        # Second upsert with different model overwrites.
        await repo.upsert(
            workspace_id=workspace_id,
            account_id=account_id,
            settings=EmbeddingSettings(model="text-embedding-3-small"),
        )
        parsed = await repo.get(workspace_id=workspace_id, account_id=account_id)
        assert parsed is not None
        assert parsed.model == "text-embedding-3-small"

    async def test_get_missing_returns_none(self, session, workspace_id, account_id):
        repo = EmbeddingSettingsRepository(session)
        assert await repo.get(workspace_id=workspace_id, account_id=account_id) is None
