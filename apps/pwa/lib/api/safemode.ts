/** Safe Mode API — REAL backend `GET /api/v1/safemode/queue` (backend/api/v1/
 *  safemode.py). Read-only here: pending outbound deliveries awaiting founder
 *  approval. Approve/deny POSTs are a later chunk. */

import { apiFetch } from "./client";
import type { SafeModeItem } from "./types";

/** Pending Safe Mode items awaiting founder approval (newest first). */
export function listSafeModeQueue(): Promise<SafeModeItem[]> {
  return apiFetch<SafeModeItem[]>("/api/v1/safemode/queue");
}
