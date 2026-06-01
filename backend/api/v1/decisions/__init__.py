"""``/api/v1/decisions`` aggregator router — canonicalization queue (Lift M1).

Decomposes the 349-LOC ``decisions.py`` god-file into thin endpoint-grouping
sub-modules per v8 §20 + D35 (API endpoints are thin adapters: parse →
app service → serialize):

* :mod:`.list_get` — proposal queue (``GET ""``) + decisions log (``GET /log``).
* :mod:`.resolve` — accept (``POST /{proposal_id:path}/accept``) and reject
  (``POST /{proposal_id:path}/reject``) — the founder-approval write surface.

Shared response models live in :mod:`._schemas`; the per-workspace vault
root + canonicalization service / index wiring live in :mod:`._helpers`.

Both the list and the resolve endpoints read/write the workspace **vault**
(FS-as-SoT) through the SAME vault-scoped service — proposals are markdown
notes in the vault, NOT rows in the (currently producer-less)
``canonicalization_proposals`` DB table. Sourcing both list + resolve from the
vault makes the queue address ONE store, so a listed proposal id round-trips
straight back into accept/reject.

The proposal id is the proposal's vault path; it is matched against the same
per-workspace vault root every other knowledge component uses
(``<knowledge_vault_root>/<region>/<workspace_id>/``), so a path that doesn't
belong to the caller's workspace is simply not found there → 404. Workspace
isolation is therefore structural.

Re-exports: ``_vault_root`` is imported by sibling endpoints
(:mod:`backend.api.v1.inside` + :mod:`backend.api.v1.checkpoints`), and the
``build_canonicalization_index`` / ``build_canonicalization_service``
dependency builders are imported by the test suite for
``app.dependency_overrides``. The package re-exports them so all those call
sites keep working unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import list_get, resolve
from ._helpers import (
    _vault_root,
    build_canonicalization_index,
    build_canonicalization_service,
)

# Single aggregator router — each sub-module owns its own APIRouter; we extend
# the aggregator's ``routes`` rather than using ``include_router`` to keep the
# empty-path ``GET ""`` route legal under the v1 aggregator's ``/decisions``
# prefix (same constraint §17.9 deliverables documented).
router = APIRouter()
for _sub in (list_get.router, resolve.router):
    router.routes.extend(_sub.routes)

__all__ = [
    "_vault_root",
    "build_canonicalization_index",
    "build_canonicalization_service",
    "router",
]
