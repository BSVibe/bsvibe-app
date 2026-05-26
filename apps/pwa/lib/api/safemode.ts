/** Safe Mode API — REAL backend (backend/api/v1/safemode.py):
 *   GET  /api/v1/safemode/queue                 — list pending held deliveries
 *   GET  /api/v1/safemode/queue/by-run          — group pending items by Run (B12a)
 *   POST /api/v1/safemode/{id}/approve          — approve + dispatch the held delivery
 *   POST /api/v1/safemode/{id}/deny             — deny (no dispatch), with a reason
 *   POST /api/v1/safemode/runs/{runId}/approve  — approve ALL pending items for a run (B12a)
 *
 *  The founder's first real "Decide" action: a held outbound delivery is
 *  resolved from the Brief's "Needs you" strip. */

import { apiFetch } from "./client";
import type {
  SafeModeActionResponse,
  SafeModeItem,
  SafeModeResolvedItem,
  SafeModeRunApproveResponse,
  SafeModeRunGroup,
} from "./types";

/** Pending Safe Mode items awaiting founder approval (newest first). */
export function listSafeModeQueue(): Promise<SafeModeItem[]> {
  return apiFetch<SafeModeItem[]>("/api/v1/safemode/queue");
}

/** Decided Safe Mode deliveries (approved / denied / expired), newest-decided
 *  first — the delivery side of the Decisions "Resolved" tab. */
export function listResolvedSafeMode(): Promise<SafeModeResolvedItem[]> {
  return apiFetch<SafeModeResolvedItem[]>("/api/v1/safemode/resolved");
}

/** Approve a held delivery — flips it to approved AND dispatches it out. The
 *  approve endpoint takes no body. */
export function approveSafeModeItem(itemId: string): Promise<SafeModeActionResponse> {
  return apiFetch<SafeModeActionResponse>(`/api/v1/safemode/${itemId}/approve`, {
    method: "POST",
  });
}

/** Deny a held delivery — flips it to denied, nothing is dispatched. The deny
 *  endpoint requires a JSON body (`{ reason }`; the backend schema is
 *  extra=forbid), so we always send one — empty reason by default. */
export function denySafeModeItem(itemId: string, reason = ""): Promise<SafeModeActionResponse> {
  return apiFetch<SafeModeActionResponse>(`/api/v1/safemode/${itemId}/deny`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

/** B12a — list pending Safe Mode items grouped by Run (Workflow §1.2). The
 *  Decisions surface uses the per-run groups to offer an "Approve all (N)"
 *  shortcut for multi-artifact runs. */
export function listSafeModeQueueByRun(): Promise<SafeModeRunGroup[]> {
  return apiFetch<SafeModeRunGroup[]>("/api/v1/safemode/queue/by-run");
}

/** B12a — approve ALL pending Safe Mode items for one Run together. Safe Mode
 *  is the per-Run transactional container for a multi-artifact run's
 *  accumulated partial Deliver events; this endpoint dispatches all of them
 *  through the same outbound code path the per-item approve uses. Returns the
 *  number of items approved + dispatched. */
export function approveSafeModeRun(runId: string): Promise<SafeModeRunApproveResponse> {
  return apiFetch<SafeModeRunApproveResponse>(`/api/v1/safemode/runs/${runId}/approve`, {
    method: "POST",
  });
}
