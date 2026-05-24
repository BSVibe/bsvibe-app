/** Decisions API — REAL backend (backend/api/v1/decisions.py): the
 *  canonicalization proposal queue.
 *   GET  /api/v1/decisions?status_filter=pending  — pending merge proposals
 *   POST /api/v1/decisions/{proposal_path}/accept — apply the linked actions
 *   POST /api/v1/decisions/{proposal_path}/reject — resolve without applying
 *
 *  Path note: accept/reject address a proposal by its vault path (a `:path`
 *  route converter, e.g. `proposals/merge-concepts/<file>.md`), URL-encoded
 *  whole (the backend test uses `urllib.parse.quote(path, safe="")`). The
 *  DB-backed list (`ProposalResponse`) exposes `id` (uuid) and `action_path`;
 *  `action_path` is the only path-like handle the list surfaces, so we pass it
 *  back as the resolve handle. See the PR description for the backend-side
 *  list/path contract gap this works around. */

import { apiFetch } from "./client";
import type { AcceptResponse, DecisionLogEntry, Proposal, RejectResponse } from "./types";

/** Pending canonicalization proposals — the founder-approval queue. */
export function listPendingProposals(limit = 50): Promise<Proposal[]> {
  return apiFetch<Proposal[]>(`/api/v1/decisions?status_filter=pending&limit=${limit}`);
}

/** Resolved decisions — the founder-approval audit trail (`GET /decisions/log`).
 *  Feeds the "Resolved" tab; each row records its outcome (decision_kind). */
export function listDecisionsLog(limit = 50): Promise<DecisionLogEntry[]> {
  return apiFetch<DecisionLogEntry[]>(`/api/v1/decisions/log?limit=${limit}`);
}

/** Accept a queued proposal — applies every linked typed action (e.g. the merge
 *  that collapses a variant onto its canonical anchor). `proposalPath` is the
 *  proposal's vault path; we URL-encode it whole for the `:path` route. */
export function acceptProposal(proposalPath: string): Promise<AcceptResponse> {
  return apiFetch<AcceptResponse>(`/api/v1/decisions/${encodeURIComponent(proposalPath)}/accept`, {
    method: "POST",
  });
}

/** Reject a queued proposal without applying anything. The backend
 *  RejectRequest is extra=forbid with an optional `reason`; we always send a
 *  body so the wire shape is stable (empty reason by default). */
export function rejectProposal(proposalPath: string, reason = ""): Promise<RejectResponse> {
  return apiFetch<RejectResponse>(`/api/v1/decisions/${encodeURIComponent(proposalPath)}/reject`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}
