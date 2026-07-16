/** Knowledge API — REAL backend `/api/v1/inside` (backend/api/v1/inside.py):
 *  the founder's window into what the AI has learned, and the founder-issued
 *  retract write path. (Correct / in-place field rewrite is not available yet —
 *  the backend endpoint returns 501, so there is no client for it here.)
 *   GET  /api/v1/inside/concepts                     — canonical anchors,
 *                                                       newest first, `limit`
 *                                                       (default 50, ≤200)
 *   GET  /api/v1/inside/observations                 — recent garden
 *                                                       observations (raw,
 *                                                       unpromoted), newest
 *                                                       first, `limit` (default
 *                                                       25, ≤100)
 *   GET  /api/v1/inside/graph                        — the workspace knowledge
 *                                                       graph (`{ nodes, edges }`
 *                                                       for the force-directed
 *                                                       view; sparse → empty)
 *   GET  /api/v1/inside/concepts/{id}                — inspect detail for one
 *                                                       canonical concept
 *   POST /api/v1/inside/nodes/{node_ref}/retract     — open a retract for a
 *                                                       garden note (queued,
 *                                                       undo-able 30s)
 *   POST /api/v1/inside/corrections/{id}/undo        — undo a queued
 *                                                       correction inside the
 *                                                       30s window
 *
 *  The lists default to the backend's own defaults so a plain call asks for
 *  exactly the calm snapshot it serves. */

import { apiFetch } from "./client";
import type {
  Concept,
  ConceptDetail,
  KnowledgeGraph,
  KnowledgeNote,
  Observation,
  RetractRequestBody,
  RetractResponse,
  UndoCorrectionResponse,
} from "./types";

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

/** R12 — one vault note's full content (title + body, frontmatter stripped) for
 *  the report's note viewer. `path` is the vault-relative note path; the backend
 *  404s for a path outside the note dirs / a traversal / a missing file. */
export function getNote(path: string): Promise<KnowledgeNote> {
  return apiFetch<KnowledgeNote>(`/api/v1/inside/note?path=${encodeURIComponent(path)}`);
}

/** Path-encode a `node_ref` for the retract/correct endpoints. `node_ref` is a
 *  vault-relative path (`garden/seedling/foo.md`) which the backend mounts via
 *  the `:path` converter — so `/` MUST stay literal, only the other unsafe
 *  characters (`?`, `#`, `%`, spaces, …) get encoded. `encodeURIComponent`
 *  is too aggressive (it eats `/`), so we re-allow the slashes after. */
function encodeNodeRef(nodeRef: string): string {
  return encodeURIComponent(nodeRef).replace(/%2F/gi, "/");
}

/** Open a retract for a garden note. Queued — the tombstone is committed when
 *  the 30s undo window closes (or sooner if a subsequent call to
 *  `undoCorrection` cancels it). Idempotent on `body.correction_id`.
 *
 *  The retract endpoint validates the node exists in the caller's vault and
 *  404s otherwise; this surfaces as an `ApiError` with `status=404`. */
export function retractNode(
  nodeRef: string,
  body: RetractRequestBody = {},
): Promise<RetractResponse> {
  return apiFetch<RetractResponse>(`/api/v1/inside/nodes/${encodeNodeRef(nodeRef)}/retract`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Undo a queued retraction / correction inside the 30s window. The wire
 *  contract returns the terminal status (`undone` / `expired` / `already_*`)
 *  for the UI toast to render into "Restored." / "Undo window expired."
 *  Idempotent: a second call after `undone` is `already_undone`. */
export function undoCorrection(correctionId: string): Promise<UndoCorrectionResponse> {
  return apiFetch<UndoCorrectionResponse>(
    `/api/v1/inside/corrections/${encodeURIComponent(correctionId)}/undo`,
    { method: "POST" },
  );
}
