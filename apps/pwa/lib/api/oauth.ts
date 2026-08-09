/** Browser-OAuth consent API — REAL backend `/api/oauth/*`
 *  (backend/api/oauth.py).
 *
 *  These wrap the two endpoints the PWA-hosted consent screen needs:
 *
 *  * `getOAuthClientByClientId` — public client metadata for the
 *    "Allow {client_name}…" UI. No auth (the client_id is in the URL
 *    the user is already looking at).
 *  * `postOAuthAuthorize` — commits the consent. Sends the Supabase
 *    Bearer (via the shared `apiFetch`) + the original OAuth params +
 *    `action=approve|deny`. Returns `{redirect_to}` — the caller does
 *    `window.location.href = redirect_to` to hop to the OAuth client's
 *    loopback callback (`http://localhost:49921/callback?code=…`).
 *
 *  See `apps/pwa/app/oauth/consent/page.tsx` for the consumer. */

import { apiFetch } from "./client";

/** Public-facing subset of an OAuth client row — what the consent
 *  screen renders. Mirrors backend `PublicClientResponse`. */
export interface OAuthClientPublic {
  client_id: string;
  client_name: string;
  client_type: string;
  redirect_uris: string[];
  allowed_scopes: string[];
}

/** Response shape for the PWA consent POST. The PWA uses the
 *  `redirect_to` value as `window.location.href` to bounce back to the
 *  OAuth client's loopback callback (or to the same client's redirect
 *  URI with `?error=access_denied` on Deny). */
export interface AuthorizeRedirect {
  redirect_to: string;
}

/** OAuth params the consent screen forwards from the original GET. */
export interface AuthorizeParams {
  response_type: string;
  client_id: string;
  redirect_uri: string;
  scope?: string;
  state?: string;
  code_challenge: string;
  code_challenge_method: string;
  resource?: string;
}

/** Fetch the client metadata used to render "Allow {client_name}…".
 *  Throws `ApiError` (404) when the client is unknown or revoked. */
export function getOAuthClientByClientId(clientId: string): Promise<OAuthClientPublic> {
  return apiFetch<OAuthClientPublic>(
    `/api/oauth/clients/by-client-id/${encodeURIComponent(clientId)}`,
  );
}

/** Commit the founder's Allow/Deny decision against the backend.
 *
 *  Sends `Accept: application/json` so the backend returns the JSON
 *  `{redirect_to}` shape (a cross-origin fetch can't follow a 302 to
 *  the OAuth client's loopback callback — the JS does the final hop).
 *  The body is `application/x-www-form-urlencoded` to match the
 *  existing FastAPI form route. */
export async function postOAuthAuthorize(
  params: AuthorizeParams,
  action: "approve" | "deny",
): Promise<AuthorizeRedirect> {
  const body = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") body.set(k, v);
  }
  body.set("action", action);
  return apiFetch<AuthorizeRedirect>("/api/oauth/authorize", {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      Accept: "application/json",
    },
    body: body.toString(),
  });
}

// ---------------------------------------------------------------------------
// RFC 8628 device authorization — the browser half
// ---------------------------------------------------------------------------
// The device has no browser and cannot receive a redirect, so consent happens
// here instead: the human types the short `user_code` shown on the device, sees
// what is being granted, and approves. The device is polling `/token` and picks
// the credential up on its own — nothing is ever pasted back.

/** What the consent screen needs to describe the request.
 *
 *  Deliberately carries NO `device_code`: the browser half of this flow must
 *  never be able to complete the device half. */
export interface DeviceRequest {
  client_id: string;
  scope: string[];
  status: "pending" | "approved" | "denied" | "expired" | "consumed";
  expires_at: string;
}

export interface DeviceDecision {
  status: "approved" | "denied";
  client_id: string;
  scope: string[];
}

export function getDeviceRequest(userCode: string): Promise<DeviceRequest> {
  return apiFetch<DeviceRequest>(`/api/v1/oauth/device?user_code=${encodeURIComponent(userCode)}`);
}

export function decideDeviceRequest(userCode: string, approve: boolean): Promise<DeviceDecision> {
  return apiFetch<DeviceDecision>("/api/v1/oauth/device/approve", {
    method: "POST",
    body: JSON.stringify({ user_code: userCode, approve }),
  });
}
