/** Auth API — REAL backend `/api/auth/*` (backend/api/auth/routes.py). */

import { clearSession, getSession, setSession } from "@/lib/auth/session";
import { getAccount } from "./account";
import { apiFetch } from "./client";
import type { SupabaseSession } from "./types";

/** Password login against Supabase via the backend. Persists the session, then
 *  best-effort fetches the personal account id (`/api/v1/account`) and stores
 *  it on the session so subsequent calls carry `X-BSVibe-Account-Id`. The
 *  account fetch is defensive: a failure does NOT block login — the backend's
 *  require_account_id fallback covers the missing header. */
export async function login(email: string, password: string): Promise<void> {
  const session = await apiFetch<SupabaseSession>("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
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
