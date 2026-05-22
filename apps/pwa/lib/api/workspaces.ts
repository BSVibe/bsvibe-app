/** Workspaces API — REAL backend `/api/v1/workspaces`. */

import { apiFetch } from "./client";
import type { Workspace } from "./types";

/** Every workspace the caller has an active membership in. */
export function listWorkspaces(): Promise<Workspace[]> {
  return apiFetch<Workspace[]>("/api/v1/workspaces");
}
