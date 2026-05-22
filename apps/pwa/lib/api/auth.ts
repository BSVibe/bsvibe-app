/** Auth API — REAL backend `/api/auth/*` (backend/api/auth/routes.py). */

import { clearSession, setSession } from "@/lib/auth/session";
import { apiFetch } from "./client";
import type { SupabaseSession } from "./types";

/** Password login against Supabase via the backend. Persists the session. */
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
