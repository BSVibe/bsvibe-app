/** Messages API — REAL backend `POST /api/v1/messages` (backend/api/v1/
 *  messages.py). The Direct path entrypoint: a founder-direct submission lands
 *  on the workflow exactly as an inbound webhook would. Returns a 202 acceptance
 *  receipt, NOT a run — the run is created by the worker pipeline. */

import { apiFetch } from "./client";
import type { AskResult, MessageAccepted, MessageCreate } from "./types";

/** Submit a founder-direct message. `product_id` is optional (workspace-wide
 *  when omitted). A double-submit of the same text collapses server-side and
 *  comes back with `duplicate: true`. */
export function submitMessage(body: MessageCreate): Promise<MessageAccepted> {
  return apiFetch<MessageAccepted>("/api/v1/messages", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** L10 — ask a Direct *question* and get an inline answer. REAL backend
 *  `POST /api/v1/messages/ask`. `answered: false` means the text is a work
 *  request (or no chat model is configured) → the caller should dispatch it via
 *  `submitMessage` instead. A question is answered synchronously (no run, no
 *  executor). `productId` (optional) grounds the answer in that product's
 *  deliverables + knowledge so "how's the project?" reflects real state. */
export function askMessage(text: string, productId?: string | null): Promise<AskResult> {
  return apiFetch<AskResult>("/api/v1/messages/ask", {
    method: "POST",
    body: JSON.stringify({ text, ...(productId ? { product_id: productId } : {}) }),
  });
}
