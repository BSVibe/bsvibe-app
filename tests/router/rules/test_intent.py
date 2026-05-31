"""IntentClassifier — embedding-based classification via VectorSearchBackend."""

from __future__ import annotations

import uuid

from backend.embedding.storage.backend import VectorEntry
from backend.embedding.storage.memory import InMemoryVectorBackend
from backend.router.rules.intent import IntentClassifier, IntentSpec

WS = uuid.uuid4()
ACCT = uuid.uuid4()


async def _backend_with(*entries: VectorEntry) -> InMemoryVectorBackend:
    backend = InMemoryVectorBackend()
    await backend.upsert(entries)
    return backend


class _FakeEmbedder:
    """Records the most recent text and returns a deterministic vector.

    The vector mirrors the call ``text`` letter — used to make
    similarity match testable without a real model.
    """

    def __init__(self, vec_for: dict[str, list[float]]) -> None:
        self._table = vec_for
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return self._table.get(text, [0.0, 0.0])


class TestClassify:
    async def test_returns_intent_when_above_threshold(self):
        intent_id = uuid.uuid4()
        backend = await _backend_with(
            VectorEntry(
                id=uuid.uuid4(),
                workspace_id=WS,
                account_id=ACCT,
                intent_id=intent_id,
                embedding=[1.0, 0.0],
                embedding_model="m",
            )
        )
        embedder = _FakeEmbedder({"hi": [1.0, 0.0]})
        clf = IntentClassifier(
            embedder=embedder,
            backend=backend,
            intents=[IntentSpec(id=intent_id, name="support", threshold=0.5)],
            workspace_id=WS,
            account_id=ACCT,
            embedding_model="m",
        )
        assert await clf.classify("hi") == "support"
        assert embedder.calls == ["hi"]

    async def test_returns_none_below_threshold(self):
        intent_id = uuid.uuid4()
        backend = await _backend_with(
            VectorEntry(
                id=uuid.uuid4(),
                workspace_id=WS,
                account_id=ACCT,
                intent_id=intent_id,
                embedding=[1.0, 0.0],
                embedding_model="m",
            )
        )
        # Orthogonal query → cosine 0 < 0.5 threshold.
        embedder = _FakeEmbedder({"weird": [0.0, 1.0]})
        clf = IntentClassifier(
            embedder=embedder,
            backend=backend,
            intents=[IntentSpec(id=intent_id, name="support", threshold=0.5)],
            workspace_id=WS,
            account_id=ACCT,
            embedding_model="m",
        )
        assert await clf.classify("weird") is None

    async def test_picks_higher_score_when_multiple_match(self):
        good = uuid.uuid4()
        bad = uuid.uuid4()
        backend = await _backend_with(
            VectorEntry(
                id=uuid.uuid4(),
                workspace_id=WS,
                account_id=ACCT,
                intent_id=bad,
                embedding=[0.5, 0.5],
                embedding_model="m",
            ),
            VectorEntry(
                id=uuid.uuid4(),
                workspace_id=WS,
                account_id=ACCT,
                intent_id=good,
                embedding=[1.0, 0.0],
                embedding_model="m",
            ),
        )
        embedder = _FakeEmbedder({"x": [1.0, 0.0]})
        clf = IntentClassifier(
            embedder=embedder,
            backend=backend,
            intents=[
                IntentSpec(id=bad, name="bad", threshold=0.3),
                IntentSpec(id=good, name="good", threshold=0.3),
            ],
            workspace_id=WS,
            account_id=ACCT,
            embedding_model="m",
        )
        assert await clf.classify("x") == "good"

    async def test_empty_text_returns_none(self):
        backend = InMemoryVectorBackend()
        embedder = _FakeEmbedder({})
        clf = IntentClassifier(
            embedder=embedder,
            backend=backend,
            intents=[],
            workspace_id=WS,
            account_id=ACCT,
            embedding_model="m",
        )
        assert await clf.classify("") is None
        assert embedder.calls == []

    async def test_no_intents_returns_none(self):
        backend = InMemoryVectorBackend()
        embedder = _FakeEmbedder({"hi": [1.0, 0.0]})
        clf = IntentClassifier(
            embedder=embedder,
            backend=backend,
            intents=[],
            workspace_id=WS,
            account_id=ACCT,
            embedding_model="m",
        )
        assert await clf.classify("hi") is None
        # No intents → no point embedding either.
        assert embedder.calls == []

    async def test_per_intent_threshold(self):
        intent_id = uuid.uuid4()
        backend = await _backend_with(
            VectorEntry(
                id=uuid.uuid4(),
                workspace_id=WS,
                account_id=ACCT,
                intent_id=intent_id,
                embedding=[1.0, 0.0],
                embedding_model="m",
            )
        )
        # cosine = 0.707 — passes threshold 0.5 but fails 0.9.
        embedder = _FakeEmbedder({"x": [0.7071, 0.7071]})

        loose = IntentClassifier(
            embedder=embedder,
            backend=backend,
            intents=[IntentSpec(id=intent_id, name="support", threshold=0.5)],
            workspace_id=WS,
            account_id=ACCT,
            embedding_model="m",
        )
        strict = IntentClassifier(
            embedder=embedder,
            backend=backend,
            intents=[IntentSpec(id=intent_id, name="support", threshold=0.9)],
            workspace_id=WS,
            account_id=ACCT,
            embedding_model="m",
        )
        assert await loose.classify("x") == "support"
        assert await strict.classify("x") is None
