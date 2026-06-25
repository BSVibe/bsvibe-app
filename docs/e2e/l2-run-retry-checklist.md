# E2E — L2 Failed runs are recoverable, not dead-ends (#9)

Founder pain: a run that ends `failed` / `cancelled` showed "완료하지 못했어요.
지금 하실 일은 없어요." with no explanation and no way forward.

## Backend (automated — `tests/glue/test_run_retry.py`, `tests/api/test_checkpoints_executor_actions.py`)
- [x] `POST /runs/{id}/retry` on a FAILED run → 200, run → OPEN, `retry_count` bumped
- [x] retry on a CANCELLED run → OPEN
- [x] retry records a re-open history row
- [x] retry on a non-terminal run (open/running/review_ready/shipped) → 409
- [x] retry cross-workspace / unknown id → 404
- [x] `GET /runs/{id}/detail` surfaces `failure_reason` for a FAILED run; `null` when running
- [x] `verification_failed` / `human_review_required` Decisions offer a `retry` action
- [x] resolving a `verification_failed` Decision with `action_key=retry` re-opens the run (RUNNING → OPEN)

## PWA (automated — `apps/pwa/test/run-detail.test.tsx`)
- [x] a failed run renders a Retry button (not a dead-end line)
- [x] a failed run surfaces WHY it stopped when a reason is recorded
- [x] clicking Retry POSTs `/api/v1/runs/{id}/retry` and reloads

## Prod dogfood (manual — verify at final review)
- [ ] Open a failed/stood-down run → see the failure reason + a "다시 시도" button →
      click → run re-opens (OPEN) and the worker re-drives it.
- [ ] On a `verification_failed` Decision card → a "다시 시도" action appears between
      Approve & ship and Discard → clicking it re-opens the run for another attempt.
