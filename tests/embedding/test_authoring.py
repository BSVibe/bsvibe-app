"""Intent authoring application service — create def + embed examples.

Shared by the REST endpoint and the MCP tool (Lift N2). The embedder is
injected so no real embedding API is ever called: tests pass a deterministic
stub, or None to exercise the no-embedding-config path (examples land with
``embedding=None`` and surface in ``list_examples_needing_reembedding``).
"""

from __future__ import annotations

import uuid

import pytest

from backend.embedding.authoring import (
    IntentAuthoringDuplicateError,
    IntentNotFoundError,
    create_intent_with_examples,
    delete_intent,
)
from backend.embedding.repository import IntentRepository
from backend.embedding.service import EmbeddedExample

pytestmark = pytest.mark.asyncio


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def account_id() -> uuid.UUID:
    return uuid.uuid4()


class _StubEmbedder:
    """Deterministic embedder — one fixed vector per text, tagged model."""

    def __init__(self, model: str = "stub/embed-1") -> None:
        self.model = model
        self.calls: list[str] = []

    async def embed_one(self, text: str) -> EmbeddedExample:
        self.calls.append(text)
        # Cheap deterministic vector; length + content don't matter here.
        vec = [float(len(text)), 1.0, 0.0]
        return EmbeddedExample(text=text, embedding=vec, model=self.model)


async def test_create_intent_embeds_examples(session, workspace_id, account_id):
    embedder = _StubEmbedder()
    intent = await create_intent_with_examples(
        session,
        workspace_id=workspace_id,
        account_id=account_id,
        name="marketing",
        threshold=0.7,
        examples=["write a launch tweet", "draft a blog post"],
        embedder=embedder,
    )
    await session.commit()

    assert intent.name == "marketing"
    assert intent.threshold == 0.7
    assert embedder.calls == ["write a launch tweet", "draft a blog post"]

    repo = IntentRepository(session)
    examples = await repo.list_examples(workspace_id=workspace_id, account_id=account_id)
    assert {e.text for e in examples} == {"write a launch tweet", "draft a blog post"}
    # Every example carries a stored vector tagged with the embedder's model.
    assert all(e.embedding is not None for e in examples)
    assert all(e.embedding_model == "stub/embed-1" for e in examples)


async def test_create_intent_without_embedder_is_graceful(session, workspace_id, account_id):
    """No embedding model configured -> intent + examples still created with
    ``embedding=None`` (nothing lost); they surface as needing re-embedding."""
    intent = await create_intent_with_examples(
        session,
        workspace_id=workspace_id,
        account_id=account_id,
        name="design",
        threshold=0.65,
        examples=["make a logo"],
        embedder=None,
    )
    await session.commit()

    assert intent.name == "design"
    repo = IntentRepository(session)
    examples = await repo.list_examples(workspace_id=workspace_id, account_id=account_id)
    assert [e.text for e in examples] == ["make a logo"]
    assert examples[0].embedding is None

    # Surfaces for a later re-embedding pass.
    pending = await repo.list_examples_needing_reembedding(
        workspace_id=workspace_id, account_id=account_id, active_model="anything"
    )
    assert [e.text for e in pending] == ["make a logo"]


async def test_create_intent_no_examples(session, workspace_id, account_id):
    """Empty example list is allowed — just the definition."""
    intent = await create_intent_with_examples(
        session,
        workspace_id=workspace_id,
        account_id=account_id,
        name="empty",
        threshold=0.65,
        examples=[],
        embedder=None,
    )
    await session.commit()
    assert intent.name == "empty"
    repo = IntentRepository(session)
    examples = await repo.list_examples(workspace_id=workspace_id, account_id=account_id)
    assert examples == []


async def test_create_duplicate_name_raises(session, workspace_id, account_id):
    await create_intent_with_examples(
        session,
        workspace_id=workspace_id,
        account_id=account_id,
        name="dup",
        threshold=0.65,
        examples=[],
        embedder=None,
    )
    await session.commit()
    with pytest.raises(IntentAuthoringDuplicateError):
        await create_intent_with_examples(
            session,
            workspace_id=workspace_id,
            account_id=account_id,
            name="dup",
            threshold=0.65,
            examples=[],
            embedder=None,
        )


async def test_create_embedding_failure_stores_none(session, workspace_id, account_id):
    """A provider hiccup (embedding=None from the service) must not lose the
    example — it lands with ``embedding=None`` like the no-config path."""

    class _FailingEmbedder:
        model = "stub/embed-1"

        async def embed_one(self, text: str) -> EmbeddedExample:
            return EmbeddedExample(text=text, embedding=None, model=self.model)

    intent = await create_intent_with_examples(
        session,
        workspace_id=workspace_id,
        account_id=account_id,
        name="flaky",
        threshold=0.65,
        examples=["some phrase"],
        embedder=_FailingEmbedder(),
    )
    await session.commit()
    assert intent.name == "flaky"
    repo = IntentRepository(session)
    examples = await repo.list_examples(workspace_id=workspace_id, account_id=account_id)
    assert examples[0].embedding is None


async def test_delete_intent_removes_examples(session, workspace_id, account_id):
    embedder = _StubEmbedder()
    intent = await create_intent_with_examples(
        session,
        workspace_id=workspace_id,
        account_id=account_id,
        name="gone",
        threshold=0.65,
        examples=["a", "b"],
        embedder=embedder,
    )
    await session.commit()

    await delete_intent(
        session, intent_id=intent.id, workspace_id=workspace_id, account_id=account_id
    )
    await session.commit()

    repo = IntentRepository(session)
    intents = await repo.list_intents(workspace_id=workspace_id, account_id=account_id)
    assert intents == []
    examples = await repo.list_examples(workspace_id=workspace_id, account_id=account_id)
    assert examples == []


async def test_delete_unknown_intent_raises(session, workspace_id, account_id):
    with pytest.raises(IntentNotFoundError):
        await delete_intent(
            session,
            intent_id=uuid.uuid4(),
            workspace_id=workspace_id,
            account_id=account_id,
        )
