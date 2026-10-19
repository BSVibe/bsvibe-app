# E2E — a run whose output went out before REVIEW_READY still ships

Incident 2026-10-19: run `4f5e9bfb` (scheduled BStockReport weekly report, `output_mode: direct`). Its `emit_deliverable` reached Telegram at 00:31 while the run was RUNNING. `auto_resolve_run_on_delivery` only completes a REVIEW_READY run, so it did nothing. The run reached REVIEW_READY at 00:35, and nothing asked again. It stayed at "needs review" with its output already delivered.

Root cause: delivery success was an event handled at one moment, never a fact anyone could look up later.

## What changed

- `deliverables.delivered_at` records the first successful dispatch. Migration `deliverable_delivered_at` backfills it from `created_at` for rows that carry compensation handles, since handles are written only for successful outbound actions.
- `run_delivery_resolution.ship_if_delivered(run)` ships a run when all of these hold:
  - the run is REVIEW_READY with no local ship path recorded;
  - it has a delivered, unretracted deliverable;
  - it has no queued delivery event, no Safe Mode item still pending, and no pending Decision.
- The check runs in two places, so neither the order of events nor a session race can strand a run:
  - at the REVIEW_READY transition (`AgentRunner.transition`);
  - in a sweep on every DeliveryWorker tick (`ship_delivered_review_ready_runs`).

## Live checks

- [ ] **The stranded run completes after deploy** ⭐
      `bsvibe_runs_show 4f5e9bfb-fdf7-4337-bc4e-3341052018d8` should go `review_ready → shipped` within one delivery-worker tick after the deploy. Look for log `run_shipped_after_earlier_delivery run_id=4f5e9bfb…`.
      If it stays, check its deliverable: no compensation handle means no backfill. Then discard it by hand.
- [ ] **No second send**: the Telegram chat gets nothing new for that report when the run ships.
- [ ] **Next weekly report (10/26 09:30 KST)** ends at `shipped` without any human action, and sends exactly one Telegram message.
- [ ] **A github-bound run is not shipped early**: a run that emits a partial and then opens a PR stays REVIEW_READY until its PR delivery (or merge, if watched).

## Automated cover

- `tests/glue/test_run_ships_after_early_delivery.py` (10). Runs on SQLite and on Postgres with fail-closed RLS (36 with the neighbouring resolution suites). Covers:
  - the stamp, and a failed dispatch leaving it unset;
  - the prod order (delivered while RUNNING, then REVIEW_READY ships);
  - controls: nothing delivered, a queued event, a pending approval, an already-decided approval, a retracted delivery;
  - the sweep, including local-ship and nothing-delivered controls, plus idempotence;
  - the worker tick wiring.

  Each of the 8 production edges was cut once and turned a test red.
- The migration backfill was checked on a throwaway Postgres. A row with a handle got `delivered_at`; NULL and `[]` rows did not.
