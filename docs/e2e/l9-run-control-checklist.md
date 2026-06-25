# E2E — L9 Run control: stop an in-flight task + reset clock on retry

Founder feedback: (1) there was no way to STOP a task that is in-flight; (2) on
retry the elapsed "작업 시간" kept counting from the FIRST start instead of resetting.

## Backend (automated — `tests/glue/test_run_cancel.py`)
- [x] `POST /runs/{id}/cancel` on OPEN / RUNNING → CANCELLED
- [x] cancel on a terminal run (shipped/failed/cancelled) → 409
- [x] cancel cross-workspace / unknown → 404
- [x] cooperative cancel: after cancel, the worker's post-drive transition CANNOT
      flip the run back to a terminal success (transition guard)
- [x] the guard STILL allows the explicit retry path (CANCELLED → OPEN)
- [x] retry stamps `restarted_at`; `GET /runs/{id}` exposes it
- [x] M1 route-set guard updated for the new `POST /{run_id}/cancel`

## PWA (automated — `run-detail.test.tsx`, `brief-data.test.tsx`)
- [x] an in-flight run shows a Stop button; clicking POSTs `/cancel`
- [x] a terminal run shows NO Stop button
- [x] a retried run's elapsed clock starts at `restarted_at`, not `created_at`

## Prod dogfood (manual — verify at final review)
- [ ] Open a RUNNING run → "중단 / Stop" button → click → run flips to cancelled
      (and the in-flight work stops affecting it; the Retry affordance appears).
- [ ] Retry a failed/cancelled run → the "Working on now" elapsed ("Xm in") resets
      to the retry moment, not the original start.
- [ ] A cancelled run can be retried (re-opened) — cancel is not a dead-end.
