/**
 * Thin fetch wrapper for the backend API.
 *
 * Calls go directly to the backend at `NEXT_PUBLIC_BACKEND_URL` (cross-origin;
 * the backend serves CORS). When the env var is unset (vitest, and as a safe
 * default) the base is empty, so requests stay relative (`/api/*`). The
 * caller's Supabase access token is attached as a Bearer header from the
 * session store.
 */

import { clearSession, getSession, setSession } from "@/lib/auth/session";
import type { SupabaseSession } from "./types";

/**
 * Backend base URL. Prod build: `https://api.bsvibe.dev`. Unset → "" so the
 * path stays relative (preserves fetch-mocked tests that intercept `/api/...`).
 */
const base = process.env.NEXT_PUBLIC_BACKEND_URL ?? "";

/**
 * The backend base URL as an ABSOLUTE url, for surfaces that must show the
 * founder a copy-pasteable value (e.g. the executor-worker install command's
 * `BSVIBE_WORKER_SERVER_URL`). Unlike `base` (which is "" so requests stay
 * relative), this falls back to the prod URL so the snippet is never blank /
 * pointed at the worker's localhost default.
 */
export function backendBaseUrl(): string {
  return process.env.NEXT_PUBLIC_BACKEND_URL || "https://api.bsvibe.dev";
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * Shared "session expired" handler. A 401 on an authenticated read means the
 * Supabase token expired / was revoked: clear the session and bounce to
 * `/login` exactly once. `apiFetch` is not a React component, so the redirect
 * goes through `window.location.assign` (a hard nav that also discards stale
 * in-memory state). Loop guards keep a transient single 401 from cascading:
 *   - never fire for the auth endpoints themselves (`/api/auth/*`) — a 401 there
 *     is a failed login, not an expired session;
 *   - no-op when there is no session (already logged out);
 *   - no-op when we are already on `/login` (no redundant nav).
 */
function handleUnauthorized(path: string): void {
  if (path.startsWith("/api/auth/")) return;
  if (!getSession()) return;

  clearSession();

  if (typeof window === "undefined") return;
  if (window.location.pathname === "/login") return;
  window.location.assign("/login");
}

/** Refresh the access token once it is within this window of expiring. */
const EXPIRY_SKEW_MS = 60_000;

/**
 * One shared in-flight refresh. Supabase refresh tokens are SINGLE-USE /
 * rotating — two concurrent refreshes would burn the token and 401 every
 * subsequent call — so concurrent callers await the SAME promise. Resolves
 * `true` once a rotated session is persisted, `false` when there is nothing to
 * refresh or the backend rejected the refresh token.
 */
let refreshInFlight: Promise<boolean> | null = null;

function refreshSession(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight;
  const session = getSession();
  if (!session?.refreshToken) return Promise.resolve(false);

  refreshInFlight = (async () => {
    try {
      const res = await fetch(`${base}/api/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: session.refreshToken }),
      });
      if (!res.ok) return false;
      const next = (await res.json()) as SupabaseSession;
      const current = getSession();
      setSession({
        accessToken: next.access_token,
        refreshToken: next.refresh_token,
        email: next.email,
        userId: next.supabase_user_id,
        expiresAt: Date.now() + next.expires_in * 1000,
        personalAccountId: current?.personalAccountId,
      });
      return true;
    } catch {
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}

/** Build request headers from the CURRENT session (re-read after any refresh). */
function authHeaders(init: RequestInit): Headers {
  const session = getSession();
  const headers = new Headers(init.headers);
  if (!headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (session) {
    headers.set("Authorization", `Bearer ${session.accessToken}`);
    // The billing-account axis (orthogonal to the workspace). When the session
    // carries the personal account id, send it so account-scoped routes
    // (/api/v1/accounts) resolve without relying on the backend fallback.
    if (session.personalAccountId) {
      headers.set("X-BSVibe-Account-Id", session.personalAccountId);
    }
  }
  return headers;
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const isAuthPath = path.startsWith("/api/auth/");

  // Proactive — refresh a near-expired token before spending a request on a
  // guaranteed 401. Never for /api/auth/* (those mint/refresh the session).
  if (!isAuthPath) {
    const session = getSession();
    if (session?.refreshToken && session.expiresAt - Date.now() < EXPIRY_SKEW_MS) {
      await refreshSession();
    }
  }

  let response = await fetch(`${base}${path}`, { ...init, headers: authHeaders(init) });

  // Reactive — a 401 on a non-auth path with a session means the token expired
  // or was rotated out from under us. Refresh once and retry before logging out.
  if (response.status === 401 && !isAuthPath && getSession()) {
    if (await refreshSession()) {
      response = await fetch(`${base}${path}`, { ...init, headers: authHeaders(init) });
    }
  }

  if (!response.ok) {
    // A still-401 means the session is expired/invalid AND a refresh could not
    // save it: clear and redirect once (loop-guarded in handleUnauthorized).
    // Non-401 failures just surface their status so callers (e.g. the Brief's
    // calm placeholder fallback) can decide — no logout-cascade on blips.
    if (response.status === 401) {
      handleUnauthorized(path);
    }
    throw new ApiError(response.status, `${init.method ?? "GET"} ${path} → ${response.status}`);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}
