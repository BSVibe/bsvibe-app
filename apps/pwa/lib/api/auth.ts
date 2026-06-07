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
/** sessionStorage key carrying the post-sign-in destination across the
 *  Supabase IdP round-trip. Set atomically by `startOAuth` immediately before
 *  the IdP hand-off; read + cleared by `/auth/callback`. Lift E11 dropped the
 *  hash-fragment encoding because Supabase strips/overwrites fragments in
 *  practice — sessionStorage survives same-tab cross-origin navigation, which
 *  is the only mode an OAuth round-trip uses. */
export const RETURN_TO_KEY = "bsvibe.return_to";

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

/** Same-origin path guard, shared between /login's URL-param accept gate and
 *  `startOAuth`'s defense-in-depth check on the caller-supplied return_to.
 *  Rejects anything that isn't a relative path beginning with a single `/`
 *  — `//evil.com/x` is protocol-relative and resolves cross-origin.
 *
 *  Exported so the consent client, /login, and /auth/callback all derive
 *  their "is this safe" answer from one source of truth (Lift E11). */
export function isSameOriginPath(raw: string | null | undefined): raw is string {
  if (!raw) return false;
  if (!raw.startsWith("/")) return false;
  if (raw.startsWith("//")) return false;
  return true;
}

/** Start social sign-in: derive a PKCE verifier (stashed for the return trip),
 *  ask the backend for the GoTrue authorize URL with the matching challenge,
 *  then hand the browser off to it. The provider sends the user back to
 *  `/auth/callback?code=…`, where `completeOAuth` finishes the exchange.
 *
 *  Lift E11 — `returnTo` is stashed in **sessionStorage**, never on the URL.
 *  Earlier shapes (hash fragment, query param) all failed in practice:
 *
 *   * **Hash fragment** — Supabase's GoTrue rebuilds the redirect URL via
 *     `url.Parse` + `query.Encode()` + `.String()`. The fragment SOMETIMES
 *     survives, but in dogfood (2026-06-06, qazasa123 Google sign-in) it
 *     was provably dropped between the IdP 302 and `/auth/callback`. The
 *     mechanism is too fragile to depend on.
 *   * **Query param** — Supabase's redirect URL allow-list is exact-match
 *     on path+query. A callback URL with `?return_to=…` fails the match
 *     and Supabase falls back to the Site URL (`/brief`), losing context.
 *
 *  sessionStorage survives same-tab cross-origin navigation by spec; the
 *  full OAuth round-trip never opens a new tab. We commit the value
 *  ATOMICALLY here — the line after `setItem` is `window.location.assign`,
 *  so no React re-render or subsequent setState can clear it between
 *  intent and hand-off. */
export async function startOAuth(provider: OAuthProvider, returnTo?: string): Promise<void> {
  // Defense in depth: refuse to stash an unsafe target even if a caller
  // forgot to guard. `/login`'s `safeReturnTo` is the primary gate, but a
  // crafted future entry point shouldn't be able to bypass us.
  if (returnTo !== undefined && !isSameOriginPath(returnTo)) {
    throw new Error("unsafe return_to rejected");
  }
  const verifier = randomVerifier();
  sessionStorage.setItem(PKCE_VERIFIER_KEY, verifier);
  sessionStorage.setItem(PKCE_PROVIDER_KEY, provider);
  const codeChallenge = await challengeFor(verifier);
  const callbackUrl = `${window.location.origin}/auth/callback`;
  const { authorize_url } = await apiFetch<{ authorize_url: string }>(
    `/api/auth/oauth/${provider}/authorize`,
    {
      method: "POST",
      body: JSON.stringify({
        code_challenge: codeChallenge,
        redirect_to: callbackUrl,
      }),
    },
  );
  // Atomic with the assign — write, then leave. Any further work (a stray
  // setState, an unmount cleanup) cannot run between these two lines
  // because `window.location.assign` synchronously commits to a navigation.
  if (returnTo) {
    sessionStorage.setItem(RETURN_TO_KEY, returnTo);
  } else {
    // Vanilla sign-in (no consent flow). Make sure a stale value from an
    // earlier aborted run can't leak into this one.
    sessionStorage.removeItem(RETURN_TO_KEY);
  }
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
