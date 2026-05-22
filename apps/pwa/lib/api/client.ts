/**
 * Thin fetch wrapper for the backend API.
 *
 * All calls go to same-origin `/api/*`, which Next rewrites to the backend
 * (next.config.ts). The caller's Supabase access token is attached as a
 * Bearer header from the session store.
 */

import { getSession } from "@/lib/auth/session";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const session = getSession();
  const headers = new Headers(init.headers);
  if (!headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (session) {
    headers.set("Authorization", `Bearer ${session.accessToken}`);
  }

  const response = await fetch(path, { ...init, headers });

  if (!response.ok) {
    // Surface the status; the session store / gate decides on redirect. We do
    // not auto-clear on 401 here to avoid logout cascades on transient blips.
    throw new ApiError(response.status, `${init.method ?? "GET"} ${path} → ${response.status}`);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}
