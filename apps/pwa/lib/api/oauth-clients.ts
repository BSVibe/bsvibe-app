/** OAuth client management — REAL backend `/api/v1/oauth/clients`
 *  (backend/api/oauth.py).
 *
 *  The founder registers external OAuth clients (e.g. Claude Code's MCP
 *  driver) from Settings → Developer. The returned `client_id` is what
 *  the founder pastes into the external app's config — the OAuth flow
 *  itself (authorize / token / refresh / revoke) is browser-mediated and
 *  doesn't go through this lib. */

import { apiFetch } from "./client";

export interface OAuthClient {
  id: string;
  client_id: string;
  client_name: string;
  client_type: string;
  redirect_uris: string[];
  allowed_scopes: string[];
  created_at: string;
  revoked_at: string | null;
}

export interface CreateOAuthClientRequest {
  client_name: string;
  redirect_uris: string[];
  allowed_scopes?: string[];
}

export function listOAuthClients(): Promise<OAuthClient[]> {
  return apiFetch<OAuthClient[]>("/api/v1/oauth/clients");
}

export function createOAuthClient(payload: CreateOAuthClientRequest): Promise<OAuthClient> {
  return apiFetch<OAuthClient>("/api/v1/oauth/clients", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function deleteOAuthClient(clientId: string): Promise<void> {
  return apiFetch<void>(`/api/v1/oauth/clients/${encodeURIComponent(clientId)}`, {
    method: "DELETE",
  });
}
