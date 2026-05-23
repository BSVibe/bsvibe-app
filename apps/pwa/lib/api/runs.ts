/** Runs API — REAL backend `GET /api/v1/runs` (backend/api/v1/runs.py).
 *  Read-only: ExecutionRun rows for the active workspace, newest first. */

import { apiFetch } from "./client";
import type { Run } from "./types";

/** Recent ExecutionRun rows for the active workspace (newest first). */
export function listRuns(limit = 50): Promise<Run[]> {
  return apiFetch<Run[]>(`/api/v1/runs?limit=${limit}`);
}
