/** Deliverables API — REAL backend `GET /api/v1/deliverables`
 *  (backend/api/v1/deliverables.py). Read-only: Deliverable rows produced by a
 *  verified run for the active workspace, newest first. The Brief's "Recently
 *  shipped" reads this to surface real artifact detail. */

import { apiFetch } from "./client";
import type { ArtifactContent, Deliverable, DeliverableReport } from "./types";

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

/** The produced CONTENT of one artifact file, read-only. REAL backend
 *  `GET /api/v1/deliverables/{id}/artifacts/{ref:path}`. `ref` MUST be one of
 *  the deliverable's own `artifact_refs` — the backend whitelists it and 404s
 *  anything else (unknown ref, traversal, cleaned run dir). A 404 surfaces as
 *  an `ApiError`; the viewer renders a calm "content unavailable — see git".
 *  The `ref` may contain slashes (`src/app.ts`); each segment is encoded so the
 *  `:path` route still resolves it as one nested path, not a query/escape. */
export function getDeliverableArtifact(
  deliverableId: string,
  ref: string,
): Promise<ArtifactContent> {
  const encodedRef = ref
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");
  return apiFetch<ArtifactContent>(
    `/api/v1/deliverables/${encodeURIComponent(deliverableId)}/artifacts/${encodedRef}`,
  );
}
