/** Safe Mode API — REAL backend (backend/api/v1/safemode.py):
 *   GET  /api/v1/safemode/queue                 — list pending held deliveries
 *   POST /api/v1/safemode/{id}/approve          — approve + dispatch the held delivery
 *   POST /api/v1/safemode/{id}/deny             — deny (no dispatch), with a reason
 *
 *  The founder's first real "Decide" action: a held outbound delivery is
 *  resolved from the Brief's "Needs you" strip. */

import { apiFetch } from "./client";
import type { SafeModeActionResponse, SafeModeItem } from "./types";

/** Pending Safe Mode items awaiting founder approval (newest first). */
export function listSafeModeQueue(): Promise<SafeModeItem[]> {
  return apiFetch<SafeModeItem[]>("/api/v1/safemode/queue");
}

/** Approve a held delivery — flips it to approved AND dispatches it out. The
 *  approve endpoint takes no body. */
export function approveSafeModeItem(itemId: string): Promise<SafeModeActionResponse> {
  return apiFetch<SafeModeActionResponse>(`/api/v1/safemode/${itemId}/approve`, {
    method: "POST",
  });
}

/** Which ACT a denial is. Only `rejected_approach` teaches the next run — a
 *  `queue_cleanup` denial ("same run's intermediate snapshot; not a content
 *  problem") is swept out of the queue without becoming negative knowledge.
 *  Mirrors backend `DenyKind`; the backend requires the field (no default,
 *  because either default is silently wrong in one direction). */
export type DenyKind = "rejected_approach" | "queue_cleanup";

/** Deny a held delivery — flips it to denied, nothing is dispatched. The deny
 *  endpoint requires a JSON body (`{ reason, kind }`; the backend schema is
 *  extra=forbid), so we always send one — empty reason by default.
 *
 *  The row's plain Decline is a judgement about the delivery, so it sends
 *  `rejected_approach` — the same act the phone's reject tap records. It
 *  carries no reason text, so it teaches nothing either way (the backend's
 *  founder-authored-text gate still applies). */
export function denySafeModeItem(
  itemId: string,
  reason = "",
  kind: DenyKind = "rejected_approach",
): Promise<SafeModeActionResponse> {
  return apiFetch<SafeModeActionResponse>(`/api/v1/safemode/${itemId}/deny`, {
    method: "POST",
    body: JSON.stringify({ reason, kind }),
  });
}
