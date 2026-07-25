"""Cross-context wire-contract vocabulary — run/schedule ``kind`` literals.

A ``kind`` string is a *wire contract* carried on schedule rows and on the
run payloads they seed. It is produced in one bounded context (Schedule)
and consumed in another (Workflow's workers), so its single definition
lives here, in the shared kernel, rather than in either context — a leaf
module both may import without either depending on the other.

This module imports nothing from any bounded context (it satisfies the
"common leaves do not import bounded contexts" import-linter contract) and
is therefore also reachable from the MCP context, which may not import
:mod:`backend.schedule`.
"""

from __future__ import annotations

#: ``product_tick`` — an autonomous cadence tick: the founder sets only the
#: WHEN (per product), and BSVibe decides WHAT to do. Seeded by the schedule
#: emitter onto a run's ``payload["kind"]`` and read by the workflow workers
#: to route the resulting deliverable through Safe Mode.
SCHEDULE_KIND_PRODUCT_TICK = "product_tick"

__all__ = [
    "SCHEDULE_KIND_PRODUCT_TICK",
]
