/** Knowledge API — REAL backend `/api/v1/inside` (backend/api/v1/inside.py):
 *  the founder's read-only window into what the AI has learned. The deployed
 *  surface was relabeled "Knowledge" (지식); the backend router keeps the
 *  `/inside` prefix (it ships with the same surface), so these paths are
 *  unchanged.
 *   GET /api/v1/inside/concepts      — canonical anchors (settled concepts),
 *                                       newest first, `limit` (default 50, ≤200)
 *   GET /api/v1/inside/observations  — recent garden observations (raw,
 *                                       unpromoted), newest first, `limit`
 *                                       (default 25, ≤100)
 *   GET /api/v1/inside/graph         — the workspace knowledge graph as
 *                                       nodes + edges for the force-directed
 *                                       view (`{ nodes: [], edges: [] }` for a
 *                                       fresh/sparse workspace)
 *
 *  All read-only — there is no write path on this surface. The backend clamps
 *  `limit` to its per-list range; we default to the backend's own defaults so a
 *  plain call asks for exactly the calm snapshot it serves. */

import { apiFetch } from "./client";
import type { Concept, ConceptDetail, KnowledgeGraph, Observation } from "./types";

/** The workspace's canonical anchors (settled concepts), newest first.
 *  Backend default limit is 50 (clamped 1..200). */
export function listConcepts(limit = 50): Promise<Concept[]> {
  return apiFetch<Concept[]>(`/api/v1/inside/concepts?limit=${limit}`);
}

/** Inspect one concept — identity, related concepts (graph neighbours with
 *  weight), and the source observations that reference it. Read-only; the
 *  backend 404s when the id is not an active concept. */
export function getConceptDetail(id: string): Promise<ConceptDetail> {
  return apiFetch<ConceptDetail>(`/api/v1/inside/concepts/${encodeURIComponent(id)}`);
}

/** Recent garden observation notes (raw, unpromoted), newest first.
 *  Backend default limit is 25 (clamped 1..100). */
export function listObservations(limit = 25): Promise<Observation[]> {
  return apiFetch<Observation[]>(`/api/v1/inside/observations?limit=${limit}`);
}

/** The workspace knowledge graph (nodes + edges) for the force-directed view.
 *  A fresh/sparse workspace yields `{ nodes: [], edges: [] }`. */
export function getKnowledgeGraph(): Promise<KnowledgeGraph> {
  return apiFetch<KnowledgeGraph>("/api/v1/inside/graph");
}
