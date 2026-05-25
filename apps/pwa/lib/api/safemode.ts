/** Safe Mode API — REAL backend (backend/api/v1/safemode.py):
 *   GET  /api/v1/safemode/queue           — list pending held deliveries
 *   POST /api/v1/safemode/{id}/approve    — approve + dispatch the held delivery
 *   POST /api/v1/safemode/{id}/deny       — deny (no dispatch), with a reason
 *
 *  The founder's first real "Decide" action: a held outbound delivery is
 *  resolved from the Brief's "Needs you" strip. */

import { apiFetch } from "./client";
import type { SafeModeActionResponse, SafeModeItem, SafeModeResolvedItem } from "./types";

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
