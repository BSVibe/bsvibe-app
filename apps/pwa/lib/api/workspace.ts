/** Workspace metadata — REAL backend `/api/v1/workspace` (singular,
 *  backend/api/v1/workspace.py). Surfaces the workspace's id + editable name
 *  for the Settings → General "Workspace name" field, replacing the previous
 *  fallback-only behavior (the email-as-workspace-name placeholder that the
 *  /impeccable audit's Lift 13 flagged as confusing for new users).
 *
 *  Lift E2 — `default_account_id` is the workspace-wide fallback the dispatch
 *  resolver routes to when no RunRoutingRule matches a caller. Settings →
 *  Models renders the picker for it; clearing the value (null) means "no
 *  fallback — surface NoMatchingRouteError to the founder". BSVibe NEVER
 *  auto-stamps it (founder policy `bsvibe-no-implicit-routing`).
 *
 *  Distinct from `lib/api/workspaces.ts` (plural — membership lookup across
 *  workspaces) and `lib/api/account.ts` (the billing-account axis). */

import { apiFetch } from "./client";

/** The active workspace's basic facts + dispatch-resolver fallback pointer. */
export interface WorkspaceInfo {
  id: string;
  name: string;
  audit_retention_days?: number | null;
  /** Lift E2 — workspace-default ModelAccount.id for the dispatch resolver
   *  fallback. `null` = the founder has not picked one yet (resolver
   *  hard-fails on unmatched rules). */
  default_account_id?: string | null;
  /** #6 — the language LLM-generated user-facing prose is written in
   *  (knowledge notes, decision questions, framing). The Settings Language
   *  control sets this alongside the client locale. Defaults "en". */
  language?: string;
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

/** PATCH — set (or clear with `null`) the workspace-default ModelAccount the
 *  dispatch resolver falls back to. The backend validates the target is an
 *  active account in this workspace; clearing returns the workspace to "no
 *  fallback" so unrouted callers surface a NoMatchingRouteError instead of
 *  silently picking a model. */
export function setWorkspaceDefaultAccount(accountId: string | null): Promise<WorkspaceInfo> {
  return apiFetch<WorkspaceInfo>("/api/v1/workspace", {
    method: "PATCH",
    body: JSON.stringify({ default_account_id: accountId }),
  });
}

/** #6 — PATCH the workspace's LLM output language ("en" / "ko"), so generated
 *  prose (knowledge notes, decision questions, framing) follows the founder's
 *  language. Set by the Settings → Language control alongside the client locale. */
export function setWorkspaceLanguage(language: string): Promise<WorkspaceInfo> {
  return apiFetch<WorkspaceInfo>("/api/v1/workspace", {
    method: "PATCH",
    body: JSON.stringify({ language }),
  });
}
