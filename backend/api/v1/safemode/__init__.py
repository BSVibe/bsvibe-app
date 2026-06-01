"""``/api/v1/safemode`` aggregator router — founder approval gate (Lift M1).

Workflow §10.5 (Safe Mode) / §11.2 (deliver-side). When a workspace is in
Safe Mode the
:class:`backend.workflow.infrastructure.workers.delivery_worker.DeliveryWorker`
enqueues each verified deliverable into the
:class:`~backend.workflow.application.safe_mode_queue.SafeModeQueue` (status
``pending``) instead of dispatching it out. This surface lets the founder:

* ``GET /api/v1/safemode/queue`` + ``/queue/by-run`` + ``/resolved`` — read
  the pending queue, grouped-by-run, and the resolved audit log.
* ``POST /api/v1/safemode/{item_id}/approve`` + ``/{item_id}/deny`` — settle
  one queue item.
* ``POST /api/v1/safemode/runs/{run_id}/approve`` — approve all items for one
  Run (Safe Mode is the per-Run transactional container).

Decomposed per v8 §20 + D35 into:

* :mod:`.list_get` — the three read endpoints.
* :mod:`.mutations` — the three write / approval endpoints.

Shared response models live in :mod:`._schemas`; the dispatcher dependency
+ serialization / artifact-type helpers live in :mod:`._helpers`.

Approval re-uses the *same*
:func:`~backend.workflow.infrastructure.workers.delivery_worker.dispatch_delivery`
helper the worker calls for the Safe-Mode-off path, so there is one
outbound-dispatch code path.

Re-exports: ``get_delivery_dispatcher`` is imported by the test suite for
``app.dependency_overrides`` — the package re-exports it so existing test
import paths keep working unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import list_get, mutations
from ._helpers import get_delivery_dispatcher
from ._schemas import (
    SafeModeActionResponse,
    SafeModeDenyRequest,
    SafeModeItemResponse,
    SafeModeResolvedResponse,
    SafeModeRunApproveResponse,
    SafeModeRunGroupResponse,
)

# Single aggregator router — see deliverables/__init__.py for the
# ``routes.extend(...)`` rationale.
router = APIRouter()
for _sub in (list_get.router, mutations.router):
    router.routes.extend(_sub.routes)

__all__ = [
    "SafeModeActionResponse",
    "SafeModeDenyRequest",
    "SafeModeItemResponse",
    "SafeModeResolvedResponse",
    "SafeModeRunApproveResponse",
    "SafeModeRunGroupResponse",
    "get_delivery_dispatcher",
    "router",
]
