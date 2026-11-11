# Worker restart releases claims — E2E checklist

A deploy restarts the worker. Runs it was driving must go back to the queue at once, not sit
RUNNING for the stale-claim lease (2 × `executor_task_timeout_s`) — and the executor turn the
old worker launched must not keep editing a run a newer job now owns.

Unit coverage:

- `tests/workflow/test_worker_restart_releases_claims.py` (each production edge cut once and seen
  red).
- `tests/mcp/test_work_tools_refuse_a_lost_claim.py` — a released / re-claimed run refuses the old
  token and the file on disk is unchanged (take the claim check out of `load_run` and both go red).
- `tests/dispatch/test_adapter_ties_its_turn_to_the_job.py` — a cancelled job kills its executor
  session; the task token carries the run's claim.
- `tests/supervisor/sandbox/test_acquire_retries_a_name_conflict.py` — one sandbox name conflict is
  cleared and retried (take the retry out and it goes red).

## Live (production, after the deploy that ships this)

- [x] A deploy while a run is mid-drive: the run goes RUNNING → OPEN at shutdown (the job's cancel
      path releases it first, so `stop()`'s own cleanup step usually has nothing left to do and
      `agent_worker_released_on_stop` is NOT the evidence), or, after an ungraceful restart,
      `agent_worker_released_orphaned_claims` names that run.
      *2026-11-11, run `f33f959b`, with #1357 deployed (worker restarted by hand mid-turn):
      graceful — `executor_adapter_job_cancelled_mid_turn` → `run_worktree_checkpointed` →
      running → open at 09:53:29; `docker kill` — the new worker logged
      `agent_worker_released_orphaned_claims` naming the run 7 s after start.*
- [x] The same run is claimed again (`agent_worker_claimed_for_drive`) within a minute of the
      new worker starting — not two hours later.
      *Same run: re-claimed 2 s after the graceful restart (sandbox created with no conflict) and
      1 s after the kill (`sandbox_adopted`); the run went on to pass and ship.*
- [ ] After the restart, a tool call from the previous execution turn is refused: the backend log
      shows `mcp_work_claim_lost` for that run, no `mcp_work_tool` edit lands on it, and the new
      job's sandbox is created without `container name … already in use`.
      *Partly seen: on both restarts the new sandbox came up without a conflict and no stale edit
      landed — the cancelled turn was killed by `cancel_task`, so `mcp_work_claim_lost` (the safety
      net for a turn that survives) has not fired yet. Before #1357 (2026-11-10) the surviving turn
      edited the run and the re-drive failed on the name conflict.*
- [ ] A run paused on a founder decision during the deploy stays RUNNING and paused.
