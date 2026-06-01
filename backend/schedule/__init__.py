"""Schedule — the 5th bounded context (v8 §3.5 / D30).

Per v8's 6-context model (Router · Knowledge · Workflow · Identity ·
**Schedule** · Extensions), the Schedule context owns *when* the system
fires periodic work and the seam by which a wake-up substrate is plugged
in. It is intentionally small — three concerns:

* :mod:`backend.schedule.domain` — the published Protocols
  (:class:`ScheduleRunnerProtocol` — the wake-up substrate seam;
  :class:`ScheduleAdvancer` — the cron-algebra seam) and their no-cron
  v1 implementations (:class:`OneShotScheduleAdvancer`,
  :class:`FixedIntervalScheduleAdvancer`).
* :mod:`backend.schedule.application` — :class:`ScheduleTrigger`, the
  emitter that turns a *fire time* into a Workflow-side TriggerEvent.
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
