/** Personal access tokens — REAL backend `/api/v1/oauth/pats`
 *  (backend/api/oauth.py).
 *
 *  A PAT is the way onto `/mcp` for a client that can't finish the browser
 *  sign-in: the OAuth flow lands its callback on `http://localhost:<port>`,
 *  which only resolves when the browser and the MCP client share a machine.
 *  Over a remote tunnel, SSH, a headless box or a scheduled job there is no
 *  listener to hit, and a static bearer token is the only path.
 *
 *  The raw token exists only in the create response — the server keeps the
 *  row's id, scopes and label, never the value — so the listing type
 *  deliberately has no `token` field. */

import { apiFetch } from "./client";

export interface Pat {
  id: string;
  name: string;
  scope: string[];
  issued_at: string;
  /** `null` = never expires. */
  expires_at: string | null;
}

/** The create response, and the ONLY place the raw token is ever available. */
export interface PatCreated extends Pat {
  token: string;
}

export interface CreatePatRequest {
  name: string;
  scope?: string[];
  /** Omit for a token that never expires. */
  expires_in_days?: number;
}

export function listPats(): Promise<Pat[]> {
  return apiFetch<Pat[]>("/api/v1/oauth/pats");
}

export function createPat(payload: CreatePatRequest): Promise<PatCreated> {
  return apiFetch<PatCreated>("/api/v1/oauth/pats", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function deletePat(patId: string): Promise<void> {
  return apiFetch<void>(`/api/v1/oauth/pats/${encodeURIComponent(patId)}`, {
    method: "DELETE",
  });
}
