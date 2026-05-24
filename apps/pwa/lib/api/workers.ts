/** Workers API — REAL backend `/api/v1/workers`
 *  (backend/api/v1/workers.py): the founder's front door for registering a
 *  machine that runs the BSVibe worker process, where their coding-agent CLIs
 *  (claude_code / codex / opencode) are logged in. Registering one lets BSVibe
 *  route work to those CLIs under the founder's own subscription.
 *
 *   GET    /api/v1/workers                — list registered workers for the
 *                                           active workspace (status +
 *                                           capabilities; no full secret)
 *   POST   /api/v1/workers/install-token  — mint the one-time install token the
 *                                           founder gives the worker process;
 *                                           the `{token}` is returned ONCE
 *   DELETE /api/v1/workers/{id}           — revoke a worker, 204 No Content
 *
 *  (register / heartbeat / poll / result are for the headless worker process,
 *  not this UI.) */

import { apiFetch } from "./client";
import type { Worker, WorkerInstallToken } from "./types";

/** Registered executor workers for the active workspace. */
export function listWorkers(): Promise<Worker[]> {
  return apiFetch<Worker[]>("/api/v1/workers");
}

/** Mint the one-time install token. The response carries the plaintext `token`
 *  (a capability — show once, then it's gone): the founder runs the worker
 *  process with it on the machine where their coding-agent CLIs are logged in. */
export function mintInstallToken(): Promise<WorkerInstallToken> {
  return apiFetch<WorkerInstallToken>("/api/v1/workers/install-token", {
    method: "POST",
  });
}

/** Revoke a worker by id — the backend stops routing work to it thereafter.
 *  204 No Content, so this resolves to void. */
export function revokeWorker(workerId: string): Promise<void> {
  return apiFetch<void>(`/api/v1/workers/${encodeURIComponent(workerId)}`, {
    method: "DELETE",
  });
}
