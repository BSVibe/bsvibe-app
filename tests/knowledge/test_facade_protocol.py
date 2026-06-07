"""Lift A — Knowledge facade Protocol shape tests.

These tests assert the Knowledge Protocol *exists with the right shape*. No real
behavior — concrete implementations come in later lifts.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any, get_type_hints

import pytest

from backend.knowledge.facade import (
    CanonRetrievalQuery,
    CanonRetrievalResult,
    IngestRequest,
    IngestResult,
    Knowledge,
)


def test_knowledge_is_runtime_checkable() -> None:
    assert getattr(Knowledge, "_is_runtime_protocol", False) is True


def test_minimal_mock_conforms_to_knowledge() -> None:
    class _Mock:
        async def ingest(self, request: IngestRequest) -> IngestResult:  # noqa: ARG002
            return IngestResult(proposals_count=0, notes_count=0, run_id=uuid.uuid4())

        async def retrieve_canon(self, query: CanonRetrievalQuery) -> CanonRetrievalResult:  # noqa: ARG002
            return CanonRetrievalResult(notes=[])

        async def settle(self, *, workspace_id: uuid.UUID, region: str) -> int:  # noqa: ARG002
            return 0

    mock = _Mock()
    assert isinstance(mock, Knowledge)


def test_ingest_request_is_frozen_dataclass() -> None:
    assert dataclasses.is_dataclass(IngestRequest)
    assert IngestRequest.__dataclass_params__.frozen is True  # type: ignore[attr-defined]
    field_names = {f.name for f in dataclasses.fields(IngestRequest)}
    assert field_names == {"workspace_id", "region", "artifacts"}


def test_ingest_request_field_types() -> None:
    hints_types = get_type_hints(IngestRequest)
    assert hints_types["workspace_id"] is uuid.UUID
    assert hints_types["region"] is str
    assert hints_types["artifacts"] == list[dict[str, Any]]


def test_ingest_request_is_immutable() -> None:
    req = IngestRequest(workspace_id=uuid.uuid4(), region="us", artifacts=[])
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.region = "eu"  # type: ignore[misc]


def test_ingest_result_is_frozen_dataclass() -> None:
    assert dataclasses.is_dataclass(IngestResult)
    assert IngestResult.__dataclass_params__.frozen is True  # type: ignore[attr-defined]
    field_names = {f.name for f in dataclasses.fields(IngestResult)}
    # Lift E8 Bug 2 — IngestResult now also carries ``notes_created``,
    # ``notes_updated``, and ``chunk_failures`` so the product-bootstrap
    # runtime can decide ``failed`` vs ``complete`` from the produce-side
    # signal (a chunk that raised has chunk_failures > 0; a real no-op
    # repo has chunk_failures == 0).
    assert field_names == {
        "proposals_count",
        "notes_count",
        "run_id",
        "notes_created",
        "notes_updated",
        "chunk_failures",
    }


def test_ingest_result_field_types() -> None:
    hints_types = get_type_hints(IngestResult)
    assert hints_types["proposals_count"] is int
    assert hints_types["notes_count"] is int
    assert hints_types["run_id"] is uuid.UUID
    assert hints_types["notes_created"] is int
    assert hints_types["notes_updated"] is int
    assert hints_types["chunk_failures"] is int


def test_canon_retrieval_query_is_frozen_dataclass() -> None:
    assert dataclasses.is_dataclass(CanonRetrievalQuery)
    assert CanonRetrievalQuery.__dataclass_params__.frozen is True  # type: ignore[attr-defined]
    field_names = {f.name for f in dataclasses.fields(CanonRetrievalQuery)}
    assert field_names == {"workspace_id", "region", "seed_text", "k"}


def test_canon_retrieval_query_field_types_and_default() -> None:
    hints_types = get_type_hints(CanonRetrievalQuery)
    assert hints_types["workspace_id"] is uuid.UUID
    assert hints_types["region"] is str
    assert hints_types["seed_text"] is str
    assert hints_types["k"] is int
    query = CanonRetrievalQuery(workspace_id=uuid.uuid4(), region="us", seed_text="hi")
    assert query.k == 8


def test_canon_retrieval_result_is_frozen_dataclass() -> None:
    assert dataclasses.is_dataclass(CanonRetrievalResult)
    assert CanonRetrievalResult.__dataclass_params__.frozen is True  # type: ignore[attr-defined]
    field_names = {f.name for f in dataclasses.fields(CanonRetrievalResult)}
    assert field_names == {"notes"}


def test_canon_retrieval_result_field_types() -> None:
    hints_types = get_type_hints(CanonRetrievalResult)
    assert hints_types["notes"] == list[dict[str, Any]]
