/** Runs API — REAL backend `GET /api/v1/runs` (backend/api/v1/runs.py).
 *  Read-only: ExecutionRun rows for the active workspace, newest first. */

import { apiFetch } from "./client";
import type { Run, RunCancel, RunDetail, RunRetry } from "./types";

/** Recent ExecutionRun rows for the active workspace (newest first). */
export function listRuns(limit = 50): Promise<Run[]> {
  return apiFetch<Run[]>(`/api/v1/runs?limit=${limit}`);
}

/** The inspectable run-detail surface for one run — trigger context, paused-run
 *  decisions, the latest verification outcome, and the resulting deliverable id.
 *  REAL backend `GET /api/v1/runs/{id}/detail`. A 404 (run not in the caller's
 *  workspace / unknown id) surfaces as an `ApiError`. */
export function getRunDetail(runId: string): Promise<RunDetail> {
  return apiFetch<RunDetail>(`/api/v1/runs/${encodeURIComponent(runId)}/detail`);
}

/** Re-open a terminal-failed run for another attempt (L2 #9). REAL backend
 *  `POST /api/v1/runs/{id}/retry` — a FAILED / CANCELLED run flips back to OPEN
 *  so the worker re-picks it. A non-terminal run → 409, an unknown id → 404
 *  (both surface as `ApiError`). */
export function retryRun(runId: string): Promise<RunRetry> {
  return apiFetch<RunRetry>(`/api/v1/runs/${encodeURIComponent(runId)}/retry`, {
    method: "POST",
  });
}

/** Stop an in-flight run (L9). REAL backend `POST /api/v1/runs/{id}/cancel` —
 *  an OPEN / RUNNING run flips to CANCELLED (cooperative: the worker's in-flight
 *  drive is discarded). A terminal run → 409, an unknown id → 404. A cancelled
 *  run can be re-opened later via `retryRun`. */
export function cancelRun(runId: string): Promise<RunCancel> {
  return apiFetch<RunCancel>(`/api/v1/runs/${encodeURIComponent(runId)}/cancel`, {
    method: "POST",
  });
}
