/** Inside API — REAL backend `/api/v1/inside` (backend/api/v1/inside.py): the
 *  founder's read-only window into what the AI has learned.
 *   GET /api/v1/inside/concepts      — canonical anchors (settled concepts),
 *                                       newest first, `limit` (default 50, ≤200)
 *   GET /api/v1/inside/observations  — recent garden observations (raw,
 *                                       unpromoted), newest first, `limit`
 *                                       (default 25, ≤100)
 *
 *  Both are read-only — there is no write path on this surface. The backend
 *  clamps `limit` to its per-list range; we default to the backend's own
 *  defaults so a plain call asks for exactly the calm snapshot it serves. */

import { apiFetch } from "./client";
import type { Concept, Observation } from "./types";

/** The workspace's canonical anchors (settled concepts), newest first.
 *  Backend default limit is 50 (clamped 1..200). */
export function listConcepts(limit = 50): Promise<Concept[]> {
  return apiFetch<Concept[]>(`/api/v1/inside/concepts?limit=${limit}`);
}

/** Recent garden observation notes (raw, unpromoted), newest first.
 *  Backend default limit is 25 (clamped 1..100). */
export function listObservations(limit = 25): Promise<Observation[]> {
  return apiFetch<Observation[]>(`/api/v1/inside/observations?limit=${limit}`);
}
