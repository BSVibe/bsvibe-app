/**
 * Thin fetch wrapper for the backend API.
 *
 * Calls go directly to the backend at `NEXT_PUBLIC_BACKEND_URL` (cross-origin;
 * the backend serves CORS). When the env var is unset (vitest, and as a safe
 * default) the base is empty, so requests stay relative (`/api/*`). The
 * caller's Supabase access token is attached as a Bearer header from the
 * session store.
 */

import { clearSession, getSession } from "@/lib/auth/session";

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

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
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

  const response = await fetch(`${base}${path}`, { ...init, headers });

  if (!response.ok) {
    // A 401 means the session is expired/invalid: clear it and redirect to
    // /login once (loop-guarded in handleUnauthorized). Non-401 failures still
    // just surface their status so callers (e.g. the Brief's calm placeholder
    // fallback) can decide — we don't logout-cascade on transient blips.
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
