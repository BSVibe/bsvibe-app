"""``/api/v1/runs`` aggregator router (Lift M1 — v8 §20 Pattern A).

Decomposes the 496-LOC ``runs.py`` god-file into thin endpoint-grouping
sub-modules per v8 §20 + D35 (API endpoints are thin adapters: parse →
app service → serialize):

* :mod:`.list_get` — ``GET ""`` + ``GET /{run_id}``.
* :mod:`.detail` — ``GET /{run_id}/detail`` (the inspectable run-detail
  surface bundling trigger / decisions / verification / partial+final
  deliverables / STORY timeline).

Shared response models live in :mod:`._schemas`; defensive payload mappers +
the timeline builder live in :mod:`._helpers`.

Runs are *created* only by the agent loop / workers (Bundle G), never by an
HTTP POST. The one founder-initiated mutation is :mod:`.retry` — re-opening a
terminal-failed run (FAILED / CANCELLED → OPEN) for another attempt (L2 #9);
it never creates a run.

The single ``router`` exposed here is the same object the v1 aggregator mounts
at ``/runs`` — call sites that ``from backend.api.v1.runs import router`` keep
working unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import detail, list_get, retry

# Single aggregator router — each sub-module owns its own APIRouter and the
# aggregator merges their routes directly (same pattern §17.9 deliverables uses).
# We extend ``routes`` rather than calling :meth:`APIRouter.include_router`
# because FastAPI refuses ``include_router(child)`` when the child carries an
# empty-path route (``@router.get("")`` for ``GET /runs``) and the parent has no
# prefix. The v1 aggregator still mounts THIS router under ``/runs``, so each
# sub-router's paths land at the same final URLs as the legacy module.
router = APIRouter()
for _sub in (list_get.router, detail.router, retry.router):
    router.routes.extend(_sub.routes)

__all__ = ["router"]
