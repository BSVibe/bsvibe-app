"""Workflow bounded context — v8 §7.

Contract (Lift N-Coverage pattern #8):

* **Owns** every Request from intake through delivery — the v8 state
  machine, advisory-lock + lease serialization, agent loop execution,
  verification, safe-mode queueing, and deliverable emission.
* **Facade**: ``backend.workflow.application`` is the public surface
  (D36 invariant); ``state_machine_driver`` + the stage services are
  the entry seams.
* **Not exposed**: ``domain/`` enums + transitions and ``infrastructure/``
  adapters (advisory lock, lease, repositories, workers) are private —
  only ``application/__init__.py`` exports are public.

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

# Lift N defensive pattern #1 (v8 §22) — public surface lives in nested modules.
__all__: list[str] = []
