/**
 * Client-side session store.
 *
 * The backend `/api/auth/*` routes return Supabase tokens (raw ES256 access
 * token + refresh token). This module persists the resulting session to
 * `localStorage` and exposes it to React via `useSyncExternalStore`, which
 * gives a correct server snapshot (always `null`) so auth-gated client
 * components hydrate without a mismatch — no set-state-in-effect needed.
 *
 * Token refresh on `expiresAt` is a deliberate follow-up; presence of a
 * session is what gates the shell today.
 */

import { useSyncExternalStore } from "react";

export interface Session {
  accessToken: string;
  refreshToken: string;
  email: string | null;
  userId: string;
  /** Epoch ms when the access token expires (for future refresh). */
  expiresAt: number;
  /** The workspace's personal billing-account id, fetched from
   *  `/api/v1/account` after login and sent as `X-BSVibe-Account-Id`. Optional:
   *  the backend resolves a fallback when it's absent, so login never blocks
   *  on the fetch. */
  personalAccountId?: string;
}

const STORAGE_KEY = "bsvibe.session";
const listeners = new Set<() => void>();

function readFromStorage(): Session | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<Session>;
    if (typeof parsed?.accessToken !== "string" || typeof parsed?.userId !== "string") {
      return null;
    }
    return parsed as Session;
  } catch {
    return null;
  }
}

// Read once at module init so getSnapshot returns a stable reference.
let current: Session | null = readFromStorage();

function emit(): void {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

const neverChanges = (): (() => void) => () => {};

export function getSession(): Session | null {
  return current;
}

export function setSession(session: Session): void {
  current = session;
  if (typeof window !== "undefined") {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(session));
  }
  emit();
}

export function clearSession(): void {
  current = null;
  if (typeof window !== "undefined") {
    window.localStorage.removeItem(STORAGE_KEY);
  }
  emit();
}

/** The current session, reactively. `null` on the server + first hydration. */
export function useSession(): Session | null {
  return useSyncExternalStore(subscribe, getSession, () => null);
}

/** `false` on the server + during hydration, `true` once mounted on the
 *  client — without a state-setting effect. Used to defer auth redirects
 *  until the real client-side session is known. */
export function useHydrated(): boolean {
  return useSyncExternalStore(
    neverChanges,
    () => true,
    () => false,
  );
}
