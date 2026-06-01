"""Workflow context — infrastructure workers.

Per v8 D34, workers belong in ``<context>/infrastructure/workers/``. The
agent / intake / delivery / verifier / relay workers + the production
runtime (``run.py``) all bind Workflow domain + application logic to the
DB-poll / Redis-Streams trigger substrate.

The settle worker (BSage write subscriber) belongs to the Knowledge
context — see :mod:`backend.knowledge.infrastructure.workers.settle_worker`.
The schedule worker belongs to the Schedule context — see
:mod:`backend.schedule.infrastructure.workers.schedule_worker`. Common
infra (``base``, ``db``, ``emit``, ``streams``, ``relays``) is shared
across contexts and remains at :mod:`backend.workers`.

Multi-server safety invariants (Lift J / v8 §11)
------------------------------------------------

Every worker advance MUST be claim-or-skip safe so a second uvicorn
instance running the same DB cannot double-fire the same row. Each
worker's queue-consume site uses the mechanism prescribed in v8 §11.5:

* :mod:`agent_worker`    — ``SELECT … FOR UPDATE SKIP LOCKED`` on
  ``requests`` (claim phase) and ``execution_runs`` (drive phase).
* :mod:`intake_worker`   — ``SELECT … FOR UPDATE SKIP LOCKED`` on
  ``trigger_events`` (via :class:`IdempotencyRepository.list_undrained`).
* :mod:`verifier_worker` — ``SELECT … FOR UPDATE SKIP LOCKED`` on
  ``work_steps``.
* :mod:`delivery_worker` — ``SELECT … FOR UPDATE SKIP LOCKED`` on
  ``delivery_events`` (Lift J — added).
* :mod:`relay_worker`    — ``SELECT … FOR UPDATE SKIP LOCKED`` on
  ``audit_outbox`` (Lift J — added via OutboxStore).
* :mod:`backend.knowledge.infrastructure.workers.settle_worker`
  — ``SELECT … FOR UPDATE SKIP LOCKED`` on ``execution_run_activities``
  for the row-claim site, plus ``pg_try_advisory_lock`` keyed by
  workspace id (:mod:`backend.workflow.infrastructure.lease`) for the
  per-workspace promote site (Lift J — added).
* :mod:`backend.schedule.infrastructure.workers.schedule_worker`
  — ``SELECT … FOR UPDATE SKIP LOCKED`` on ``workspace_schedules`` for
  the row-claim, plus a unique constraint at the
  :class:`~backend.schedule.application.emitter.ScheduleTrigger.fire`
  site that collapses duplicates at the DB layer (already shipped).
* The orchestrator's per-run dispatch additionally takes
  :func:`~backend.workflow.infrastructure.advisory_lock.try_run_dispatch_lock`
  (already shipped) — a session-scoped ``pg_try_advisory_lock`` keyed
  by run id, so even if a workflow advance is racing two instances at
  the application layer the orchestrator short-circuits to a no-op.

Idempotence at the state-advance side complements the claim layer: the
``dispatch_run_attempt`` / ``dispatch_verification`` / ``dispatch_settle``
/ ``dispatch_deliverable`` driver entrypoints re-run on the same
(state, event) pair to the same outcome (see
:mod:`backend.workflow.domain.transitions` + ``state_machine_driver``).
"""

from __future__ import annotations
