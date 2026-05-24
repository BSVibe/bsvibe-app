/** Runs API — REAL backend `GET /api/v1/runs` (backend/api/v1/runs.py).
 *  Read-only: ExecutionRun rows for the active workspace, newest first. */

import { apiFetch } from "./client";
import type { Run, RunDetail } from "./types";

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
