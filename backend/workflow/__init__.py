"""Workflow bounded context — v8 §7.

The single bounded context that owns a Request's lifecycle from
``Receive`` through ``Deliver``. Built in 3 layers:

* ``domain/`` — state enums (``WorkflowState`` / ``WorkflowEvent``),
  per-domain enums (``RequestStatus`` / ``WorkStepStatus`` / ``ProofState``),
  transition matrix, value objects.
* ``application/`` — stage services + transition handlers + public surface
  (D36 — populated by Lift H2/H3).
* ``infrastructure/`` — IO adapters (advisory lock, repositories, workers).

Lift H1 establishes the skeleton (state machine + advisory lock); Lift H2
decomposes ``execution/orchestrator.py``; Lift H3 absorbs ``intake/`` +
``delivery/`` and relocates workers.

External callers MUST import from ``backend.workflow.application``
(D36 invariant). Internal helpers in ``domain/`` / ``infrastructure/``
are *not* part of the public surface — except for the per-domain enums
which retain back-compat re-exports from their pre-H1 locations during
the migration.
"""

from __future__ import annotations
