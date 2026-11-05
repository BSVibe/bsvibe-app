# Worker restart releases claims — E2E checklist

A deploy restarts the worker. Runs it was driving must go back to the queue at once, not sit
RUNNING for the stale-claim lease (2 × `executor_task_timeout_s`).

Unit coverage: `tests/workflow/test_worker_restart_releases_claims.py` (each production edge
cut once and seen red).

## Live (production, after the deploy that ships this)

- [ ] A deploy while a run is mid-drive: the worker log shows `agent_worker_released_on_stop`
      (graceful) or, after the restart, `agent_worker_released_orphaned_claims` naming that run.
- [ ] The same run is claimed again (`agent_worker_claimed_for_drive`) within a minute of the
      new worker starting — not two hours later.
- [ ] A run paused on a founder decision during the deploy stays RUNNING and paused.
