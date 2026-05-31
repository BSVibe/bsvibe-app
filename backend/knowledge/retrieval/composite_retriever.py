"""CompositeCanonRetriever — multi-source workspace knowledge retrieval (B11b).

The verifier (B3) and the orchestrator's B6 seed inject one
:class:`~backend.workflow.application.verification_service.CanonRetriever` per run. Before
B11b, that was a single :class:`~backend.knowledge.retrieval.canon_retriever.CanonConceptRetriever`
returning promoted canonical patterns — resolved decisions were invisible to
the next run, so the same question got re-asked.

This composite combines multiple :class:`CanonRetriever` sources behind the
same Protocol so existing callers see a single
``retrieve_for_signals(signals) -> list[str]`` returning canon patterns AND
resolved-decision summaries (deduped, capped, in source order).

Discipline matches the underlying retrievers: graceful-empty (no source has
anything → ``[]``), never raises into verify (one source failing degrades to
the others), workspace-scoped (each source holds its own per-workspace
binding), bounded (a hard total cap on statements).
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog

from backend.workflow.application.verification_service import CanonRetriever

logger = structlog.get_logger(__name__)

#: Hard upper bound across ALL composed sources. Each source has its own
#: source-local cap; the composite then re-caps the merged + deduped list so a
#: future run's contract / seed never balloons.
_TOTAL_CAP = 8


class CompositeCanonRetriever:
    """Compose multiple :class:`CanonRetriever` sources into one Protocol-shape.

    Source order is the iteration order — earlier sources win the cap when the
    merged list exceeds :data:`_TOTAL_CAP`. The production wiring puts the
    canon-concept retriever first (its statements are higher precision —
    structurally gated by the promoter), then the resolved-decisions retriever.
    """

    __slots__ = ("_sources",)

    def __init__(self, sources: Sequence[CanonRetriever]) -> None:
        self._sources = tuple(sources)

    async def retrieve_for_signals(self, signals: str) -> list[str]:
        """Return the merged + deduped statements from every source, capped."""
        merged: list[str] = []
        seen: set[str] = set()
        for source in self._sources:
            try:
                statements = await source.retrieve_for_signals(signals)
            except Exception:  # noqa: BLE001 — one source failing must not break others
                logger.warning(
                    "composite_canon_source_failed",
                    source=type(source).__name__,
                    exc_info=True,
                )
                continue
            for statement in statements:
                if not statement or not statement.strip():
                    continue
                key = statement.strip()
                if key in seen:
                    continue
                seen.add(key)
                merged.append(key)
                if len(merged) >= _TOTAL_CAP:
                    return merged
        return merged


__all__ = ["CompositeCanonRetriever"]
