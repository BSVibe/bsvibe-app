/** Workers API — REAL backend `/api/v1/workers`
 *  (backend/api/v1/workers.py): the founder's front door for registering a
 *  machine that runs the BSVibe worker process, where their coding-agent CLIs
 *  (claude_code / codex / opencode) are logged in. Registering one lets BSVibe
 *  route work to those CLIs under the founder's own subscription.
 *
 *   GET    /api/v1/workers        — list registered workers for the active
 *                                   workspace (status + capabilities;
 *                                   no full secret)
 *   DELETE /api/v1/workers/{id}   — revoke a worker, 204 No Content
 *
 *  Register happens host-side via `bsvibe-worker register --name X` against
 *  POST /api/v1/workers/register; the PWA never sees the worker token.
 *  (heartbeat / poll / result are for the headless worker process, not this UI.) */

import { apiFetch } from "./client";
import type { Worker } from "./types";

/** Registered executor workers for the active workspace. */
export function listWorkers(): Promise<Worker[]> {
  return apiFetch<Worker[]>("/api/v1/workers");
}

/** Revoke a worker by id — the backend stops routing work to it thereafter.
 *  204 No Content, so this resolves to void. */
export function revokeWorker(workerId: string): Promise<void> {
  return apiFetch<void>(`/api/v1/workers/${encodeURIComponent(workerId)}`, {
    method: "DELETE",
  });
}
