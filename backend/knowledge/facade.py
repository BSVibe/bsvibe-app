# bsvibe:stable-internal — modifications require a design doc update.
# Owners: knowledge/facade
"""Knowledge context — facade Protocol (Lift A).

This module defines the public surface of the future Knowledge context. No
callers are switched to it yet; concrete implementations land in subsequent
lifts which move the existing ``backend/knowledge`` ingest / retrieval /
canonicalization / graph code behind this facade.

Design source: ``~/Docs/BSVibe_Class_Architecture_Design_2026-05-30.md`` v8 §5.2.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class IngestRequest:
    workspace_id: uuid.UUID
    region: str
    artifacts: list[dict[str, Any]]


@dataclass(frozen=True)
class IngestResult:
    proposals_count: int
    notes_count: int
    run_id: uuid.UUID
    # Lift E8 Bug 2 — surface compile-time failure signal so the caller
    # (today the product-bootstrap runtime) can decide ``failed`` vs
    # ``complete`` based on whether ANY chunk actually produced notes.
    # An ingest that lost every chunk to a transport error currently looks
    # identical to a no-op (proposals_count=0, notes_count=0) — but only
    # the failure case has chunk_failures > 0.
    notes_created: int = 0
    notes_updated: int = 0
    chunk_failures: int = 0


@dataclass(frozen=True)
class CanonRetrievalQuery:
    workspace_id: uuid.UUID
    region: str
    seed_text: str
    k: int = 8


@dataclass(frozen=True)
class CanonRetrievalResult:
    notes: list[dict[str, Any]]


@runtime_checkable
class Knowledge(Protocol):
    async def ingest(self, request: IngestRequest) -> IngestResult: ...

    async def retrieve_canon(self, query: CanonRetrievalQuery) -> CanonRetrievalResult: ...

    async def settle(self, *, workspace_id: uuid.UUID, region: str) -> int: ...
