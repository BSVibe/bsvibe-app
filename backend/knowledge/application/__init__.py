"""Knowledge application layer.

Houses the :class:`~backend.knowledge.facade.Knowledge` facade concrete
(Lift I-Repo-Knowledge wires it to the existing Knowledge subsystems —
:class:`~backend.knowledge.ingest.ingest_compiler.IngestCompiler`,
:class:`~backend.knowledge.retrieval.canon_retriever.CanonConceptRetriever`,
:class:`~backend.knowledge.infrastructure.workers.settle_worker.SettleWorker`).
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
