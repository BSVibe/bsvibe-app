"""SqlAlchemyKnowledge — concrete :class:`Knowledge` facade (Lift I-Repo-Knowledge).

v8 §5.2 + D44/D45. Wires the
:class:`~backend.knowledge.facade.Knowledge` Protocol to the existing
Knowledge subsystems:

* :meth:`ingest` → :meth:`IngestCompiler.compile_batch` (adapter)
* :meth:`retrieve_canon` → :class:`CanonConceptRetriever` (adapter)
* :meth:`settle` → :meth:`SettleWorker.drain_once` (delegation)

The concrete is intentionally thin — it does not replace the underlying
services, it ROUTES through them so callers (workflow handlers, REST
endpoints, plugin runners) can depend on the Protocol surface instead of
hard-wiring to the specific implementation. This is the v8 §5.2 invariant.

Construction is via :func:`build_knowledge` (factory) so application sites
get a single ergonomic call:

    facade = build_knowledge(session=session, ...deps)
    await facade.settle(workspace_id=ws, region="us-1")

The factory is the clean DI seam — every dep is passed in once at the
construction site, and the facade itself is stateless beyond those refs.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from backend.knowledge.facade import (
    CanonRetrievalQuery,
    CanonRetrievalResult,
    IngestRequest,
    IngestResult,
)


class _SettleDrainCallable(Protocol):
    """Minimal callable shape the facade needs to drain a settle batch.

    Mirrors :meth:`backend.knowledge.infrastructure.workers.settle_worker.SettleWorker.drain_once`
    — the existing concrete returns the count of activities absorbed in one
    drain pass. The facade depends on this callable, NOT on the concrete
    worker class, so a test can inject a fake without standing up the full
    polling worker.
    """

    async def __call__(self) -> int: ...


class _IngestCallable(Protocol):
    """Callable shape the facade depends on for an ingest pass.

    Production wires this to a closure that calls
    :meth:`IngestCompiler.compile_batch` with the request's artifacts as
    :class:`~backend.knowledge.ingest.ingest_compiler.BatchItem` entries.
    Returning the (notes_created, notes_updated) pair lets the facade build
    the :class:`IngestResult` without importing the compiler module here.
    """

    async def __call__(
        self,
        *,
        workspace_id: uuid.UUID,
        region: str,
        artifacts: list[dict[str, object]],
    ) -> tuple[int, int]: ...


class _CanonRetrieveCallable(Protocol):
    """Callable shape the facade depends on for a canon retrieval pass.

    Production wires this to a closure over
    :class:`~backend.knowledge.retrieval.canon_retriever.CanonConceptRetriever`
    (or the composite). The closure receives the query parameters and
    returns the resolved notes (the existing retriever returns a list of
    statements, which the facade wraps as dicts under ``notes``).
    """

    async def __call__(
        self,
        *,
        workspace_id: uuid.UUID,
        region: str,
        seed_text: str,
        k: int,
    ) -> list[dict[str, object]]: ...


class SqlAlchemyKnowledge:
    """Concrete :class:`~backend.knowledge.facade.Knowledge` facade.

    Holds session-bound deps + callables onto the existing knowledge
    subsystems. Stateless beyond those refs; one instance per request /
    worker tick. The transaction boundary is owned by the session (D45).
    """

    __slots__ = ("_session", "_settle", "_ingest", "_retrieve")

    def __init__(
        self,
        *,
        session: AsyncSession,
        settle_callable: _SettleDrainCallable,
        ingest_callable: _IngestCallable,
        retrieve_callable: _CanonRetrieveCallable,
    ) -> None:
        self._session = session
        self._settle = settle_callable
        self._ingest = ingest_callable
        self._retrieve = retrieve_callable

    async def ingest(self, request: IngestRequest) -> IngestResult:
        notes_created, notes_updated = await self._ingest(
            workspace_id=request.workspace_id,
            region=request.region,
            artifacts=list(request.artifacts),
        )
        # ``run_id`` is a synthetic correlation id since the existing
        # ingest compiler doesn't emit one — Lift I keeps the surface
        # honest: callers that need to correlate across the facade should
        # pass a request_id-derived UUID when they construct the request.
        # The Protocol declares an opaque UUID return so any stable value
        # satisfies it.
        return IngestResult(
            proposals_count=0,
            notes_count=notes_created + notes_updated,
            run_id=uuid.uuid5(uuid.NAMESPACE_URL, f"ingest:{request.workspace_id}"),
        )

    async def retrieve_canon(self, query: CanonRetrievalQuery) -> CanonRetrievalResult:
        notes = await self._retrieve(
            workspace_id=query.workspace_id,
            region=query.region,
            seed_text=query.seed_text,
            k=query.k,
        )
        return CanonRetrievalResult(notes=notes)

    async def settle(self, *, workspace_id: uuid.UUID, region: str) -> int:
        """Drain pending settle activity into knowledge.

        The underlying :class:`SettleWorker.drain_once` is a GLOBAL drain
        (claims un-drained settle activities across every workspace,
        resolving region from each workspace row). The facade signature
        keeps the per-workspace contract from v8 §5.2 — when the global
        drain is the only available delegation, callers get back the
        full batch count and can correlate via the worker's per-row
        logs. A future split will introduce a per-workspace drain
        callable so the count strictly matches the workspace/region pair.
        """
        # ``workspace_id`` / ``region`` are signature-level invariants
        # for future per-workspace drains; today the underlying callable
        # is the global drain and we forward the count it returns.
        del workspace_id, region
        return await self._settle()


def build_knowledge(
    *,
    session: AsyncSession,
    settle_callable: Callable[[], object],
    ingest_callable: _IngestCallable,
    retrieve_callable: _CanonRetrieveCallable,
) -> SqlAlchemyKnowledge:
    """Build a :class:`SqlAlchemyKnowledge` from session-bound deps.

    Factory shape so the construction site (FastAPI dep, worker bootstrap,
    test harness) keeps the wiring explicit. ``settle_callable`` is the
    async callable that drains a settle batch — production wires this to a
    bound :meth:`SettleWorker.drain_once` (the worker is constructed
    elsewhere so its polling loop stays the canonical lifecycle).
    """
    # Cast through the Protocol — the factory's job is to accept the broader
    # callable shape callers will pass and return a Protocol-conformant
    # facade.
    settle_proto: _SettleDrainCallable = settle_callable  # type: ignore[assignment]
    return SqlAlchemyKnowledge(
        session=session,
        settle_callable=settle_proto,
        ingest_callable=ingest_callable,
        retrieve_callable=retrieve_callable,
    )


__all__ = ["SqlAlchemyKnowledge", "build_knowledge"]
