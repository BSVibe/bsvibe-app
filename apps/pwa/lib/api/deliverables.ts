/** Deliverables API — REAL backend `GET /api/v1/deliverables`
 *  (backend/api/v1/deliverables.py). Read-only: Deliverable rows produced by a
 *  verified run for the active workspace, newest first. The Brief's "Recently
 *  shipped" reads this to surface real artifact detail. */

import { apiFetch } from "./client";
import type { Deliverable, DeliverableReport } from "./types";

/** Recent Deliverable rows for the active workspace (newest first).
 *  `runId` narrows to one run's deliverables; the backend clamps `limit` to
 *  1..200. */
export function listDeliverables(limit = 50, runId?: string): Promise<Deliverable[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (runId) params.set("run_id", runId);
  return apiFetch<Deliverable[]>(`/api/v1/deliverables?${params.toString()}`);
}

/** The "glass box proof" for one deliverable — the artifact plus the
 *  verification(s) recorded for its producing run (outcome / declared contract
 *  checks / result). REAL backend `GET /api/v1/deliverables/{id}/report`. A 404
 *  (deliverable not in the caller's workspace) surfaces as an `ApiError`. */
export function getDeliverableReport(deliverableId: string): Promise<DeliverableReport> {
  return apiFetch<DeliverableReport>(
    `/api/v1/deliverables/${encodeURIComponent(deliverableId)}/report`,
  );
}
