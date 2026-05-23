/** Decisions API — REAL backend `GET /api/v1/decisions` (backend/api/v1/
 *  decisions.py). Read-only: the canonicalization proposal queue. We fetch the
 *  pending slice (the founder-approval queue) for the Brief's "Needs you". */

import { apiFetch } from "./client";
import type { Proposal } from "./types";

/** Pending canonicalization proposals — the founder-approval queue. */
export function listPendingProposals(limit = 50): Promise<Proposal[]> {
  return apiFetch<Proposal[]>(`/api/v1/decisions?status_filter=pending&limit=${limit}`);
}
