"""``/api/v1/deliverables`` aggregator router (Lift §17.9).

Decomposes the 749-LOC ``deliverables.py`` god-file into three thin
endpoint-grouping sub-modules per v8 §17.9 + D35 (API endpoints are thin
adapters: parse → app service → serialize):

* :mod:`.list_get` — read-only browse (``GET ""`` + ``GET /{id}``).
* :mod:`.proof` — verified-proof surface (``GET /{id}/report``,
  ``GET /{id}/artifacts/{ref}``).
* :mod:`.diff` — the run's captured old↔new ``git diff`` (``GET /{id}/diff``).
* :mod:`.retract` — the single mutating endpoint (``POST /{id}/retract``)
  + the :class:`RetractHandler` protocol it dispatches through.

Shared response models + payload mappers live in :mod:`._schemas`;
verified-run lookup helpers live in :mod:`._helpers`.

Read-mostly: deliverables are *produced* by the agent loop / workers on a
verified run (Bundle G), never directly created via HTTP. The PWA Brief's
"recently shipped" reads this to surface real artifacts. B12b retract is
the only path that flips ``retracted_at``.

The single ``router`` exposed here is the same object the v1 aggregator
mounts at ``/deliverables`` — call sites that ``from backend.api.v1.deliverables
import router`` (and the existing ``get_retract_handler`` test override
import) keep working unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import diff, list_get, proof, retract
from ._retract_handler import PluginRetractHandler, RetractHandler, get_retract_handler
from .retract import RetractedCompensationEntry, RetractResponse

# Single aggregator router — each sub-module owns its own APIRouter and the
# aggregator merges their routes directly. We extend the aggregator's
# ``routes`` list instead of calling :meth:`APIRouter.include_router` because
# FastAPI refuses ``include_router(child)`` when the child carries an
# empty-path route (``@router.get("")`` for ``GET /deliverables``) and the
# parent has no prefix — the legacy single-file module relied on the v1
# aggregator's ``/deliverables`` prefix to make the empty-path route legal,
# and that constraint survives the decomp. The v1 aggregator still mounts
# THIS router under ``/deliverables``, so each sub-router's paths land at
# the same final URLs as the legacy module.
router = APIRouter()
for _sub in (list_get.router, proof.router, diff.router, retract.router):
    router.routes.extend(_sub.routes)

__all__ = [
    "PluginRetractHandler",
    "RetractHandler",
    "RetractResponse",
    "RetractedCompensationEntry",
    "get_retract_handler",
    "router",
]
