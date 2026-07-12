"""IntentClassifier — semantic classify over the workspace's intents (Lift N1).

Uses the in-memory vector backend + a deterministic stub embedder (no real
embedding API). Below every intent's threshold → None (never a wrong category).
"""

from __future__ import annotations

import uuid

import pytest

from backend.embedding.storage.backend import VectorEntry
from backend.embedding.storage.memory import InMemoryVectorBackend
from backend.router.routing.run_routing.intent_classifier import (
    IntentClassifier,
    IntentSpec,
    ServiceAsEmbedder,
)

WS = uuid.uuid4()
ACCT = uuid.uuid4()
MODEL = "text-embedding-3-small"

MARKETING = IntentSpec(id=uuid.uuid4(), name="marketing", threshold=0.65)
CODING = IntentSpec(id=uuid.uuid4(), name="coding", threshold=0.65)


class _StubEmbedder:
    """Returns whatever vector the test maps a text to (default zero-ish)."""

    def __init__(self, by_text: dict[str, list[float]]) -> None:
        self._by_text = by_text

    async def embed(self, text: str) -> list[float]:
        return self._by_text.get(text, [0.0, 0.0, 1.0])


class _RaisingEmbedder:
    async def embed(self, text: str) -> list[float]:
        raise RuntimeError("embedding provider down")


async def _seeded_backend() -> InMemoryVectorBackend:
    backend = InMemoryVectorBackend()
    await backend.upsert(
        [
            VectorEntry(
                id=uuid.uuid4(),
                workspace_id=WS,
                account_id=ACCT,
                intent_id=MARKETING.id,
                embedding=[1.0, 0.0, 0.0],
                embedding_model=MODEL,
            ),
            VectorEntry(
                id=uuid.uuid4(),
                workspace_id=WS,
                account_id=ACCT,
                intent_id=CODING.id,
                embedding=[0.0, 1.0, 0.0],
                embedding_model=MODEL,
            ),
        ]
    )
    return backend


def _classifier(backend, embedder, intents=None) -> IntentClassifier:
    return IntentClassifier(
        embedder=embedder,
        backend=backend,
        intents=intents if intents is not None else [MARKETING, CODING],
        workspace_id=WS,
        account_id=ACCT,
        embedding_model=MODEL,
    )


@pytest.mark.asyncio
async def test_classifies_to_nearest_intent_above_threshold() -> None:
    backend = await _seeded_backend()
    embedder = _StubEmbedder({"run the Q3 ad campaign": [1.0, 0.0, 0.0]})
    clf = _classifier(backend, embedder)
    assert await clf.classify("run the Q3 ad campaign") == "marketing"


@pytest.mark.asyncio
async def test_picks_the_higher_similarity_intent() -> None:
    backend = await _seeded_backend()
    # Leans toward coding.
    embedder = _StubEmbedder({"refactor the auth module": [0.2, 0.98, 0.0]})
    clf = _classifier(backend, embedder)
    assert await clf.classify("refactor the auth module") == "coding"


@pytest.mark.asyncio
async def test_below_threshold_returns_none() -> None:
    backend = await _seeded_backend()
    # Orthogonal to both seeded intents → cosine 0 < 0.65.
    embedder = _StubEmbedder({"anything": [0.0, 0.0, 1.0]})
    clf = _classifier(backend, embedder)
    assert await clf.classify("anything") is None


@pytest.mark.asyncio
async def test_empty_text_or_no_intents_returns_none() -> None:
    backend = await _seeded_backend()
    embedder = _StubEmbedder({})
    assert await _classifier(backend, embedder).classify("") is None
    assert await _classifier(backend, embedder, intents=[]).classify("x") is None


@pytest.mark.asyncio
async def test_embedder_failure_degrades_to_none() -> None:
    backend = await _seeded_backend()
    clf = _classifier(backend, _RaisingEmbedder())
    assert await clf.classify("run the campaign") is None


@pytest.mark.asyncio
async def test_service_as_embedder_adapts_a_coroutine() -> None:
    async def embed_one(text: str) -> list[float]:
        return [1.0, 0.0, 0.0]

    adapter = ServiceAsEmbedder(embed_one)
    assert await adapter.embed("x") == [1.0, 0.0, 0.0]


def test_needs_classified_intent_gate() -> None:
    """The resolver only runs the classifier when a rule keys on it."""
    from backend.dispatch.resolver import _needs_classified_intent
    from backend.router.routing.run_routing.db import RunRoutingRuleRow

    def _rule(conds: list[dict]) -> RunRoutingRuleRow:
        return RunRoutingRuleRow(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            name="r",
            caller_id="workflow.frame",
            priority=10,
            is_default=False,
            target="x",
            conditions=conds,
            is_active=True,
        )

    yes = _rule([{"field": "classified_intent", "operator": "eq", "value": "m"}])
    no = _rule([{"field": "stage", "operator": "eq", "value": "design"}])
    assert _needs_classified_intent([yes]) is True
    assert _needs_classified_intent([no]) is False
    assert _needs_classified_intent([]) is False


@pytest.mark.asyncio
async def test_build_classifier_is_none_without_embedding_config() -> None:
    """No account_embedding_settings row → factory is a clean no-op."""
    import backend.embedding.db  # noqa: F401 — register embedding tables
    import backend.router.routing.run_routing.db  # noqa: F401
    from backend.config import get_settings
    from backend.router.routing.run_routing.intent_classifier import build_intent_classifier

    from ...._support import memory_session

    async with memory_session() as s:
        clf = await build_intent_classifier(
            s, get_settings(), workspace_id=uuid.uuid4(), account_id=uuid.uuid4()
        )
    assert clf is None
