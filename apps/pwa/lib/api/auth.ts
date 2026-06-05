/** Auth API — REAL backend `/api/auth/*` (backend/api/auth/routes.py). */

import { clearSession, getSession, setSession } from "@/lib/auth/session";
import { getAccount } from "./account";
import { apiFetch } from "./client";
import type { SupabaseSession } from "./types";

/** Social providers the backend assembles GoTrue authorize URLs for. Mirrors
 *  the backend `_OAUTH_PROVIDERS` allow-list. */
export type OAuthProvider = "google" | "github";

/** sessionStorage key holding the single-use PKCE verifier between the
 *  `startOAuth` redirect and the `completeOAuth` exchange. */
const PKCE_VERIFIER_KEY = "bsvibe.pkce_verifier";
/** sessionStorage key remembering which provider the redirect was for, so the
 *  `/auth/callback` page can finish the exchange against the right path. */
const PKCE_PROVIDER_KEY = "bsvibe.pkce_provider";

/** Persist a backend session, then best-effort attach the personal account id
 *  (`/api/v1/account`) so subsequent calls carry `X-BSVibe-Account-Id`. The
 *  account fetch is defensive: a failure does NOT block sign-in — the backend's
 *  require_account_id fallback covers the missing header. Shared by password
 *  login and the OAuth code exchange. */
async function persistSupabaseSession(session: SupabaseSession): Promise<void> {
  setSession({
    accessToken: session.access_token,
    refreshToken: session.refresh_token,
    email: session.email,
    userId: session.supabase_user_id,
    expiresAt: Date.now() + session.expires_in * 1000,
  });

  try {
    const account = await getAccount();
    const current = getSession();
    if (current) {
      setSession({ ...current, personalAccountId: account.id });
    }
  } catch {
    // Account discovery failed — stay logged in; the backend fallback resolves
    // the personal account server-side until a later fetch succeeds.
  }
}

/** Password login against Supabase via the backend. */
export async function login(email: string, password: string): Promise<void> {
  const session = await apiFetch<SupabaseSession>("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  await persistSupabaseSession(session);
}

/** base64url (no padding) of raw bytes — the PKCE encoding (RFC 7636). */
function base64UrlEncode(bytes: Uint8Array): string {
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Random 32-byte PKCE code verifier, base64url-encoded. */
function randomVerifier(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return base64UrlEncode(bytes);
}

/** S256 code challenge = base64url(SHA-256(verifier)). */
async function challengeFor(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64UrlEncode(new Uint8Array(digest));
}

/** Start social sign-in: derive a PKCE verifier (stashed for the return trip),
 *  ask the backend for the GoTrue authorize URL with the matching challenge,
 *  then hand the browser off to it. The provider sends the user back to
 *  `/auth/callback?code=…`, where `completeOAuth` finishes the exchange. */
export async function startOAuth(provider: OAuthProvider, returnTo?: string): Promise<void> {
  const verifier = randomVerifier();
  sessionStorage.setItem(PKCE_VERIFIER_KEY, verifier);
  sessionStorage.setItem(PKCE_PROVIDER_KEY, provider);
  const codeChallenge = await challengeFor(verifier);
  // Encode return_to into a HASH FRAGMENT (not a query param). Supabase's
  // redirect URL allow-list is exact-match on path + query; a callback URL
  // with a `?return_to=…` query param doesn't match the configured
  // `https://app.bsvibe.dev/auth/callback` and Supabase falls back to the
  // Site URL — the founder lands on /brief instead of the consent page.
  // Hash fragments are NEVER sent to the server, so the allow-list match
  // passes; the browser preserves the fragment through the 302 chain and
  // /auth/callback reads it via window.location.hash. sessionStorage is
  // also unreliable across the IdP round-trip.
  const callbackUrl = new URL(`${window.location.origin}/auth/callback`);
  if (returnTo) {
    callbackUrl.hash = `return_to=${encodeURIComponent(returnTo)}`;
  }
  const { authorize_url } = await apiFetch<{ authorize_url: string }>(
    `/api/auth/oauth/${provider}/authorize`,
    {
      method: "POST",
      body: JSON.stringify({
        code_challenge: codeChallenge,
        redirect_to: callbackUrl.toString(),
      }),
    },
  );
  window.location.assign(authorize_url);
}

/** Finish social sign-in: exchange the `?code=` for a session using the PKCE
 *  verifier stashed by `startOAuth`, then persist it. The verifier is single
 *  use — cleared once consumed. */
export async function completeOAuth(provider: OAuthProvider, code: string): Promise<void> {
  const codeVerifier = sessionStorage.getItem(PKCE_VERIFIER_KEY) ?? undefined;
  sessionStorage.removeItem(PKCE_VERIFIER_KEY);
  sessionStorage.removeItem(PKCE_PROVIDER_KEY);
  const session = await apiFetch<SupabaseSession>(`/api/auth/oauth/${provider}/callback`, {
    method: "POST",
    body: JSON.stringify({ code, code_verifier: codeVerifier }),
  });
  await persistSupabaseSession(session);
}

/** The provider a pending social sign-in was started for (set by `startOAuth`).
 *  Defaults to `"google"` when absent — the backend resolves the real provider
 *  from the code regardless, so the path segment is informational only. */
export function getPendingOAuthProvider(): OAuthProvider {
  return (sessionStorage.getItem(PKCE_PROVIDER_KEY) as OAuthProvider | null) ?? "google";
}

/** Ask the backend to email a password-recovery link. The backend always
 *  responds 204 (never leaks whether the email exists), so this resolves on any
 *  non-error response; the UI shows the same "check your inbox" either way. */
export async function requestPasswordReset(email: string): Promise<void> {
  await apiFetch<void>("/api/auth/password/reset", {
    method: "POST",
    body: JSON.stringify({
      email,
      redirect_to: `${window.location.origin}/reset-password`,
    }),
  });
}

/** Best-effort backend logout, then always clear the local session. */
export async function logout(): Promise<void> {
  try {
    await apiFetch<void>("/api/auth/logout", { method: "POST" });
  } catch {
    // Backend revocation failed — clear locally regardless.
  } finally {
    clearSession();
  }
}
