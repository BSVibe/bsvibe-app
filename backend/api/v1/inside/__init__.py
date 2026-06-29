"""``/api/v1/inside`` aggregator router (Lift M1 — v8 §20 Pattern A).

Decomposes the 573-LOC ``inside.py`` god-file into thin endpoint-grouping
sub-modules per v8 §20 + D35:

* :mod:`.concepts` — canonical anchors: ``GET /concepts`` (list) +
  ``GET /concepts/{concept_id}`` (inspector).
* :mod:`.observations` — recent garden observation notes: ``GET /observations``.
* :mod:`.graph` — the force-directed knowledge graph view: ``GET /graph``.

Shared response models live in :mod:`._schemas`; per-workspace vault
dependency builders live in :mod:`._dependencies`; body / excerpt / cap
helpers live in :mod:`._helpers`.

Read-only on the HTTP surface — every write to the vault flows through the
canonicalization queue (Workflow §5 / Bundle G). The trust ratchet
accumulates knowledge as the AI verifies work: the SettleWorker deposits raw
garden observations, and the canonicalization promoter graduates recurring
patterns into canonical anchors. The PWA "Inside" moment is the founder
peeking at what the AI has learned; this router is the backend surface that
moment reads from.

Workspace isolation is structural — every request builds an index + storage
rooted at ``<knowledge_vault_root>/<region>/<workspace_id>/`` via the shared
:func:`backend.api.v1.decisions._vault_root` helper, so another workspace's
vault is simply not there.

Re-exports: ``build_inside_storage`` + ``build_inside_index`` are imported by
the test suite for ``app.dependency_overrides`` — the package re-exports them
so existing import paths keep working.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import concepts, embeddings, graph, note, observations, retraction, trust
from ._dependencies import build_inside_graph, build_inside_index, build_inside_storage

# Single aggregator router — see deliverables/__init__.py for the rationale
# behind ``routes.extend(...)`` over ``include_router(child)`` (empty-path
# routes under a non-prefixed child router).
router = APIRouter()
for _sub in (
    concepts.router,
    observations.router,
    graph.router,
    note.router,
    retraction.router,
    trust.router,
    embeddings.router,
):
    router.routes.extend(_sub.routes)

__all__ = [
    "build_inside_graph",
    "build_inside_index",
    "build_inside_storage",
    "router",
]
