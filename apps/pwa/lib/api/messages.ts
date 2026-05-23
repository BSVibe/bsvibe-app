/** Messages API — REAL backend `POST /api/v1/messages` (backend/api/v1/
 *  messages.py). The Direct path entrypoint: a founder-direct submission lands
 *  on the workflow exactly as an inbound webhook would. Returns a 202 acceptance
 *  receipt, NOT a run — the run is created by the worker pipeline. */

import { apiFetch } from "./client";
import type { MessageAccepted, MessageCreate } from "./types";

/** Submit a founder-direct message. `product_id` is optional (workspace-wide
 *  when omitted). A double-submit of the same text collapses server-side and
 *  comes back with `duplicate: true`. */
export function submitMessage(body: MessageCreate): Promise<MessageAccepted> {
  return apiFetch<MessageAccepted>("/api/v1/messages", {
    method: "POST",
    body: JSON.stringify(body),
  });
}
