/** Workspace metadata — REAL backend `/api/v1/workspace` (singular,
 *  backend/api/v1/workspace.py). Surfaces the workspace's id + editable name
 *  for the Settings → General "Workspace name" field, replacing the previous
 *  fallback-only behavior (the email-as-workspace-name placeholder that the
 *  /impeccable audit's Lift 13 flagged as confusing for new users).
 *
 *  Distinct from `lib/api/workspaces.ts` (plural — membership lookup across
 *  workspaces) and `lib/api/account.ts` (the billing-account axis). */

import { apiFetch } from "./client";

/** The active workspace's basic facts. */
export interface WorkspaceInfo {
  id: string;
  name: string;
}

/** GET — load the active workspace. */
export function getWorkspace(): Promise<WorkspaceInfo> {
  return apiFetch<WorkspaceInfo>("/api/v1/workspace");
}

/** PATCH — rename the active workspace. Trim is applied server-side. */
export function renameWorkspace(name: string): Promise<WorkspaceInfo> {
  return apiFetch<WorkspaceInfo>("/api/v1/workspace", {
    method: "PATCH",
    body: JSON.stringify({ name }),
  });
}
