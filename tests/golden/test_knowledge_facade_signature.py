"""Golden test — pin the Knowledge facade Protocol signature (Lift N-Coverage #6).

The Knowledge facade is the public seam between every ingest / retrieve /
settle path and the vault graph + canonicalization substrate (v8 §5.2).
Any change to its public methods, parameter names, or annotations must
be deliberate — this test pins the exact shape so an accidental drift
fails CI loudly.

Source: ``backend/knowledge/facade.py`` (Lift A).
"""

from __future__ import annotations

import inspect
import uuid
from typing import Protocol, get_type_hints

from backend.knowledge.facade import (
    CanonRetrievalQuery,
    CanonRetrievalResult,
    IngestRequest,
    IngestResult,
    Knowledge,
)

# --- Protocol shape -----------------------------------------------------

EXPECTED_PUBLIC_METHODS: frozenset[str] = frozenset({"ingest", "retrieve_canon", "settle"})


def test_knowledge_protocol_public_methods_are_exactly_pinned() -> None:
    """The Knowledge Protocol exposes EXACTLY the pinned public method set.

    Adding or removing a public method on the Knowledge facade is a v8
    §5.2 contract change and requires the design doc to be updated first.
    """
    actual = frozenset(
        name
        for name in dir(Knowledge)
        if not name.startswith("_") and callable(getattr(Knowledge, name, None))
    )
    assert actual == EXPECTED_PUBLIC_METHODS, (
        "Knowledge facade public method set drift detected.\n"
        f"  expected: {sorted(EXPECTED_PUBLIC_METHODS)}\n"
        f"  actual:   {sorted(actual)}\n"
        "If this is intentional, update v8 §5.2 + this golden test."
    )


def test_knowledge_ingest_signature_is_pinned() -> None:
    """``Knowledge.ingest`` MUST be ``(self, request: IngestRequest) -> IngestResult``."""
    sig = inspect.signature(Knowledge.ingest)
    params = list(sig.parameters.values())
    assert [p.name for p in params] == ["self", "request"], (
        f"Knowledge.ingest parameter list drift: {[p.name for p in params]}"
    )

    hints = get_type_hints(Knowledge.ingest)
    assert hints.get("request") is IngestRequest, (
        f"Knowledge.ingest request annotation drift: {hints.get('request')!r}"
    )
    assert hints.get("return") is IngestResult, (
        f"Knowledge.ingest return annotation drift: {hints.get('return')!r}"
    )


def test_knowledge_retrieve_canon_signature_is_pinned() -> None:
    """``retrieve_canon`` MUST be ``(self, query: CanonRetrievalQuery) -> CanonRetrievalResult``."""
    sig = inspect.signature(Knowledge.retrieve_canon)
    params = list(sig.parameters.values())
    assert [p.name for p in params] == ["self", "query"], (
        f"Knowledge.retrieve_canon parameter list drift: {[p.name for p in params]}"
    )

    hints = get_type_hints(Knowledge.retrieve_canon)
    assert hints.get("query") is CanonRetrievalQuery
    assert hints.get("return") is CanonRetrievalResult


def test_knowledge_settle_signature_is_pinned() -> None:
    """``settle`` MUST be ``(self, *, workspace_id: uuid.UUID, region: str) -> int``."""
    sig = inspect.signature(Knowledge.settle)
    params = list(sig.parameters.values())
    # self + 2 keyword-only args.
    assert [p.name for p in params] == ["self", "workspace_id", "region"], (
        f"Knowledge.settle parameter list drift: {[p.name for p in params]}"
    )
    # workspace_id and region are keyword-only.
    kw_only_names = [p.name for p in params if p.kind == inspect.Parameter.KEYWORD_ONLY]
    assert kw_only_names == ["workspace_id", "region"], (
        f"Knowledge.settle keyword-only drift: {kw_only_names}"
    )

    hints = get_type_hints(Knowledge.settle)
    assert hints.get("workspace_id") is uuid.UUID
    assert hints.get("region") is str
    assert hints.get("return") is int


def test_knowledge_is_runtime_checkable_protocol() -> None:
    """Knowledge must remain a ``@runtime_checkable`` ``Protocol`` (v8 §5.2)."""
    assert issubclass(Knowledge, Protocol), "Knowledge must subclass Protocol"
    assert getattr(Knowledge, "_is_runtime_protocol", False), (
        "Knowledge must be @runtime_checkable so isinstance() works at the seam."
    )


# --- Dataclass shapes ---------------------------------------------------


def test_ingest_request_field_shape_is_pinned() -> None:
    hints = get_type_hints(IngestRequest)
    assert set(hints.keys()) == {"workspace_id", "region", "artifacts"}, (
        f"IngestRequest field set drift: {sorted(hints.keys())}"
    )
    assert hints["workspace_id"] is uuid.UUID
    assert hints["region"] is str


def test_ingest_result_field_shape_is_pinned() -> None:
    hints = get_type_hints(IngestResult)
    # Lift E8 Bug 2 — three new optional-default fields surface the
    # compile-time failure signal so the product-bootstrap runtime can
    # distinguish "every chunk dropped" from "no work to do".
    assert set(hints.keys()) == {
        "proposals_count",
        "notes_count",
        "run_id",
        "notes_created",
        "notes_updated",
        "chunk_failures",
    }, f"IngestResult field set drift: {sorted(hints.keys())}"
    assert hints["proposals_count"] is int
    assert hints["notes_count"] is int
    assert hints["run_id"] is uuid.UUID
    assert hints["notes_created"] is int
    assert hints["notes_updated"] is int
    assert hints["chunk_failures"] is int


def test_canon_retrieval_query_field_shape_is_pinned() -> None:
    hints = get_type_hints(CanonRetrievalQuery)
    assert set(hints.keys()) == {
        "workspace_id",
        "region",
        "seed_text",
        "k",
    }, f"CanonRetrievalQuery field set drift: {sorted(hints.keys())}"
    assert hints["workspace_id"] is uuid.UUID
    assert hints["region"] is str
    assert hints["seed_text"] is str
    assert hints["k"] is int


def test_canon_retrieval_result_field_shape_is_pinned() -> None:
    hints = get_type_hints(CanonRetrievalResult)
    assert set(hints.keys()) == {"notes"}, (
        f"CanonRetrievalResult field set drift: {sorted(hints.keys())}"
    )
