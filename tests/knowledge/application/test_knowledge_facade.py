"""Lift I-Repo-Knowledge — Knowledge facade concrete tests.

Asserts the :class:`SqlAlchemyKnowledge` concrete satisfies the
:class:`~backend.knowledge.facade.Knowledge` Protocol structurally
(``@runtime_checkable``) AND that the per-method routing reaches the
injected callables — so an upstream wiring break is caught at the seam.
"""

from __future__ import annotations

import uuid

import pytest

from backend.knowledge.application.knowledge import SqlAlchemyKnowledge, build_knowledge
from backend.knowledge.facade import (
    CanonRetrievalQuery,
    IngestRequest,
    Knowledge,
)


class _DummySession:
    """The facade doesn't touch the session in Lift I — fake is enough."""


@pytest.mark.asyncio
async def test_facade_conforms_to_protocol() -> None:
    async def _settle() -> int:
        return 0

    async def _ingest(
        *, workspace_id: uuid.UUID, region: str, artifacts: list[dict[str, object]]
    ) -> tuple[int, int]:
        return (0, 0)

    async def _retrieve(
        *, workspace_id: uuid.UUID, region: str, seed_text: str, k: int
    ) -> list[dict[str, object]]:
        return []

    facade = build_knowledge(
        session=_DummySession(),  # type: ignore[arg-type]
        settle_callable=_settle,
        ingest_callable=_ingest,
        retrieve_callable=_retrieve,
    )
    assert isinstance(facade, SqlAlchemyKnowledge)
    # @runtime_checkable Knowledge — structural conformance.
    assert isinstance(facade, Knowledge)


@pytest.mark.asyncio
async def test_settle_delegates_and_returns_count() -> None:
    calls: list[None] = []

    async def _settle() -> int:
        calls.append(None)
        return 7

    facade = build_knowledge(
        session=_DummySession(),  # type: ignore[arg-type]
        settle_callable=_settle,
        ingest_callable=_unused_ingest,
        retrieve_callable=_unused_retrieve,
    )
    count = await facade.settle(workspace_id=uuid.uuid4(), region="us-1")
    assert count == 7
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_ingest_routes_through_callable_and_builds_result() -> None:
    captured: dict[str, object] = {}

    async def _ingest(
        *, workspace_id: uuid.UUID, region: str, artifacts: list[dict[str, object]]
    ) -> tuple[int, int]:
        captured["workspace_id"] = workspace_id
        captured["region"] = region
        captured["artifacts"] = artifacts
        return (3, 2)

    facade = build_knowledge(
        session=_DummySession(),  # type: ignore[arg-type]
        settle_callable=_unused_settle,
        ingest_callable=_ingest,
        retrieve_callable=_unused_retrieve,
    )
    ws = uuid.uuid4()
    request = IngestRequest(workspace_id=ws, region="us-1", artifacts=[{"label": "a"}])
    result = await facade.ingest(request)
    assert result.notes_count == 5
    assert result.proposals_count == 0
    assert captured["workspace_id"] == ws
    assert captured["region"] == "us-1"
    assert captured["artifacts"] == [{"label": "a"}]


@pytest.mark.asyncio
async def test_retrieve_canon_routes_through_callable() -> None:
    async def _retrieve(
        *, workspace_id: uuid.UUID, region: str, seed_text: str, k: int
    ) -> list[dict[str, object]]:
        return [{"statement": "x", "k": k}]

    facade = build_knowledge(
        session=_DummySession(),  # type: ignore[arg-type]
        settle_callable=_unused_settle,
        ingest_callable=_unused_ingest,
        retrieve_callable=_retrieve,
    )
    result = await facade.retrieve_canon(
        CanonRetrievalQuery(workspace_id=uuid.uuid4(), region="us-1", seed_text="seed", k=4)
    )
    assert result.notes == [{"statement": "x", "k": 4}]


# Reusable unused-callable stubs so individual tests stay focused.
async def _unused_settle() -> int:  # pragma: no cover - guard
    raise AssertionError("settle should not be called in this test")


async def _unused_ingest(  # pragma: no cover - guard
    *, workspace_id: uuid.UUID, region: str, artifacts: list[dict[str, object]]
) -> tuple[int, int]:
    raise AssertionError("ingest should not be called in this test")


async def _unused_retrieve(  # pragma: no cover - guard
    *, workspace_id: uuid.UUID, region: str, seed_text: str, k: int
) -> list[dict[str, object]]:
    raise AssertionError("retrieve should not be called in this test")
