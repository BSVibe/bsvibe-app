/** Personal-account discovery — REAL backend `/api/v1/account` (singular,
 *  backend/api/v1/account.py). Returns the workspace's personal billing-account
 *  id, which the PWA stores on the session and sends as `X-BSVibe-Account-Id`
 *  so the model-accounts surface (`/api/v1/accounts`, plural) resolves.
 *
 *  Distinct from `lib/api/accounts.ts` (plural ModelAccount CRUD). */

import { apiFetch } from "./client";

/** The active workspace's personal billing account. */
export interface AccountInfo {
  id: string;
  workspace_id: string;
}

/** Fetch (create-on-read server-side) the personal account for the caller's
 *  active workspace. */
export function getAccount(): Promise<AccountInfo> {
  return apiFetch<AccountInfo>("/api/v1/account");
}
