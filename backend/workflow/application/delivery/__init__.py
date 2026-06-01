"""Workflow application — delivery dispatcher + per-connector adapter.

Per v8 D29 (Delivery absorption into Workflow context), the Deliver-stage
services live here:

* :class:`DeliveryDispatcher` — plugin outbound fan-out for one deliverable.
* :data:`OUTBOUND_EVENT_BUILDERS` + :func:`build_connector_delivery_adapter`
  — per-connector event shaping that adapts a verified Deliverable into the
  outbound event payload expected by each connector's ``@p.outbound``.

The SafeModeQueue + SafeModeExpirySweepRunner that gate this dispatch under
Safe Mode live one level up at :mod:`backend.workflow.application.safe_mode_queue`
and :mod:`backend.workflow.application.safe_mode_expiry` respectively.
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
