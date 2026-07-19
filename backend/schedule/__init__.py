"""Schedule — the 5th bounded context (v8 §3.5 / D30).

Contract (Lift N-Coverage pattern #8):

* **Owns** *when* the system fires periodic work — the wake-up
  substrate seam, cron-algebra seam, and the emitter that turns a fire
  time into a Workflow-side ``TriggerEvent``.
* **Facade**: published Protocols in ``backend.schedule.domain``
  (``ScheduleRunnerProtocol``, ``ScheduleAdvancer``) — v1 ships in-process
  implementations.
* **Not exposed**: infrastructure (DB-polling runner row +
  ``ScheduleWorker``) is private — callers depend on the domain
  Protocols, not the v1 polling implementation.

Per v8's 6-context model (Router · Knowledge · Workflow · Identity ·
**Schedule** · Extensions), the Schedule context owns *when* the system
fires periodic work and the seam by which a wake-up substrate is plugged
in. It is intentionally small — three concerns:

* :mod:`backend.schedule.domain` — the published Protocols
  (:class:`ScheduleRunnerProtocol` — the wake-up substrate seam;
  :class:`ScheduleAdvancer` — the cron-algebra seam) and their
  implementations: the real :class:`CronScheduleAdvancer` (the S1
  production default — recurs on a standard 5-field cron expr) plus the
  :class:`OneShotScheduleAdvancer` / :class:`FixedIntervalScheduleAdvancer`
  test/alternate seams.
* :mod:`backend.schedule.application` — :class:`ScheduleTrigger`, the
  emitter that turns a *fire time* into a Workflow-side TriggerEvent, and
  :class:`ScheduleService`, the authoring producer behind
  ``POST /api/v1/schedules``.
* :mod:`backend.schedule.infrastructure` — the persistence row
  (:class:`WorkspaceScheduleRow`), the v1 DB-polling runner
  (:class:`DbPollScheduleRunner`), and the worker shell
  (:class:`ScheduleWorker`).

Cross-context boundaries
------------------------

* The Schedule emitter writes into the **Workflow context's**
  ``trigger_events`` table (:mod:`backend.workflow.infrastructure.intake.db`)
  — schedule is a *producer* of inbound triggers; the Workflow context
  owns the inbound queue.
* The Workflow context's :class:`SafeModeExpirySweepRunner` implements
  the Schedule-domain :class:`ScheduleRunnerProtocol` — a cross-context
  dependency on the *published Protocol* (acceptable per DDD; the
  Protocol is the seam).
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
