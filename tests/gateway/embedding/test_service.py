"""EmbeddingService — provider success + graceful degradation."""

from __future__ import annotations

import pytest

from backend.gateway.embedding.service import EmbeddedExample, EmbeddingService


class _FakeProvider:
    def __init__(self, *, model: str = "test-model", fail: bool = False) -> None:
        self._model = model
        self._fail = fail

    @property
    def model(self) -> str:
        return self._model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self._fail:
            raise RuntimeError("provider down")
        return [[float(i + 1) for i in range(3)] for _ in texts]


class TestEmbedOne:
    async def test_returns_vector_on_success(self):
        svc = EmbeddingService(_FakeProvider())
        result = await svc.embed_one("hello")
        assert result.text == "hello"
        assert result.embedding == [1.0, 2.0, 3.0]
        assert result.model == "test-model"

    async def test_degrades_to_none_on_failure(self):
        svc = EmbeddingService(_FakeProvider(fail=True))
        result = await svc.embed_one("hello")
        assert result.embedding is None
        assert result.model == "test-model"


class TestEmbedMany:
    async def test_batch_returns_per_input(self):
        svc = EmbeddingService(_FakeProvider())
        results = await svc.embed_many(["a", "b", "c"])
        assert len(results) == 3
        for r in results:
            assert r.embedding == [1.0, 2.0, 3.0]

    async def test_empty_input_returns_empty(self):
        svc = EmbeddingService(_FakeProvider())
        assert await svc.embed_many([]) == []

    async def test_batch_failure_returns_all_none(self):
        svc = EmbeddingService(_FakeProvider(fail=True))
        results = await svc.embed_many(["a", "b"])
        assert len(results) == 2
        assert all(r.embedding is None for r in results)


class TestTestConnection:
    async def test_returns_dim_on_success(self):
        svc = EmbeddingService(_FakeProvider())
        dim = await svc.test_connection()
        assert dim == 3

    async def test_raises_when_provider_returns_empty(self):
        class EmptyProvider:
            model = "x"

            async def embed(self, texts):
                return [[]]

        svc = EmbeddingService(EmptyProvider())
        with pytest.raises(RuntimeError):
            await svc.test_connection()


class TestDataclass:
    def test_embedded_example_fields(self):
        e = EmbeddedExample(text="t", embedding=[1.0], model="m")
        assert e.text == "t"
        assert e.embedding == [1.0]
        assert e.model == "m"
