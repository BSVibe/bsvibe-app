/**
 * apiFetch automatic token refresh (F2).
 *
 * The Supabase access token is short-lived (~1h). Before this, apiFetch
 * attached the (possibly expired) token and logged the user out on the first
 * 401 — a session silently died mid-use after an hour. Now apiFetch:
 *   - proactively refreshes when the token is within the expiry skew, and
 *   - on a 401, refreshes once and retries the request before logging out.
 * Supabase refresh tokens are SINGLE-USE / rotating, so concurrent refreshes
 * are deduped into a single in-flight call and the rotated tokens are persisted.
 *
 * `client.ts` reads NEXT_PUBLIC_BACKEND_URL at module init, so each case stubs
 * env then imports a fresh module graph (mirrors client-401.test.ts).
 */

import type { SupabaseSession } from "@/lib/api/types";
import type { Session } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const HEALTHY: Session = {
  accessToken: "old-tok",
  refreshToken: "old-ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000, // far from expiry → no proactive refresh
};

function freshSupabaseSession(over: Partial<SupabaseSession> = {}): SupabaseSession {
  return {
    access_token: "new-tok",
    refresh_token: "new-ref",
    expires_in: 3600,
    token_type: "bearer",
    supabase_user_id: "user-1",
    email: "founder@bsvibe.dev",
    ...over,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function load() {
  const session = await import("@/lib/auth/session");
  const client = await import("@/lib/api/client");
  return { ...session, ...client };
}

function stubLocation(pathname: string) {
  const assign = vi.fn();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { pathname, assign } as unknown as Location,
  });
  return assign;
}

/**
 * A fetch mock that routes `/api/auth/refresh` to `onRefresh` and everything
 * else to `onData(callIndex)`, counting each and recording the Authorization
 * header used per data call.
 */
function installFetch(routes: {
  onData: (call: number) => Response;
  onRefresh: () => Response;
}) {
  let dataCall = 0;
  const counts = { data: 0, refresh: 0 };
  const dataAuth: string[] = [];
  global.fetch = vi.fn(async (url: unknown, init?: RequestInit) => {
    const u = String(url);
    if (u.includes("/api/auth/refresh")) {
      counts.refresh++;
      return routes.onRefresh();
    }
    dataAuth.push(new Headers(init?.headers).get("Authorization") ?? "");
    const r = routes.onData(dataCall);
    dataCall++;
    counts.data++;
    return r;
  }) as unknown as typeof fetch;
  return { counts, dataAuth };
}

describe("apiFetch token refresh", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.stubEnv("NEXT_PUBLIC_BACKEND_URL", "");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("on a 401, refreshes and retries the request once (no logout)", async () => {
    const assign = stubLocation("/brief");
    const { counts, dataAuth } = installFetch({
      onData: (call) =>
        call === 0 ? new Response("unauthorized", { status: 401 }) : jsonResponse({ ok: true }),
      onRefresh: () => jsonResponse(freshSupabaseSession()),
    });
    const { apiFetch, setSession, getSession } = await load();
    setSession(HEALTHY);

    const result = await apiFetch<{ ok: boolean }>("/api/v1/products");

    expect(result).toEqual({ ok: true });
    expect(counts.refresh).toBe(1);
    expect(counts.data).toBe(2); // original 401 + retry
    expect(dataAuth[1]).toBe("Bearer new-tok"); // retry used the rotated token
    expect(getSession()?.accessToken).toBe("new-tok");
    expect(getSession()?.refreshToken).toBe("new-ref");
    expect(assign).not.toHaveBeenCalled();
  });

  it("logs out when the refresh itself fails", async () => {
    const assign = stubLocation("/brief");
    installFetch({
      onData: () => new Response("unauthorized", { status: 401 }),
      onRefresh: () => new Response("bad refresh", { status: 401 }),
    });
    const { apiFetch, ApiError, setSession, getSession } = await load();
    setSession(HEALTHY);

    await expect(apiFetch("/api/v1/products")).rejects.toBeInstanceOf(ApiError);

    expect(getSession()).toBeNull();
    expect(assign).toHaveBeenCalledWith("/login");
  });

  it("proactively refreshes when the token is within the expiry skew", async () => {
    stubLocation("/brief");
    const { counts, dataAuth } = installFetch({
      onData: () => jsonResponse({ ok: true }),
      onRefresh: () => jsonResponse(freshSupabaseSession()),
    });
    const { apiFetch, setSession } = await load();
    setSession({ ...HEALTHY, expiresAt: Date.now() + 5_000 }); // 5s left → within skew

    const result = await apiFetch<{ ok: boolean }>("/api/v1/products");

    expect(result).toEqual({ ok: true });
    expect(counts.refresh).toBe(1);
    expect(counts.data).toBe(1); // refreshed before the call → no 401 round-trip
    expect(dataAuth[0]).toBe("Bearer new-tok");
  });

  it("dedupes concurrent refreshes into a single in-flight call (rotating tokens)", async () => {
    stubLocation("/brief");
    const { counts } = installFetch({
      onData: (call) =>
        call < 2 ? new Response("unauthorized", { status: 401 }) : jsonResponse({ ok: true }),
      onRefresh: () => jsonResponse(freshSupabaseSession()),
    });
    const { apiFetch, setSession } = await load();
    setSession(HEALTHY);

    const [a, b] = await Promise.all([
      apiFetch<{ ok: boolean }>("/api/v1/products"),
      apiFetch<{ ok: boolean }>("/api/v1/runs"),
    ]);

    expect(a).toEqual({ ok: true });
    expect(b).toEqual({ ok: true });
    expect(counts.refresh).toBe(1); // single refresh despite two concurrent 401s
  });

  it("does not refresh on /api/auth/* paths (a 401 there is a failed login)", async () => {
    stubLocation("/login");
    const { counts } = installFetch({
      onData: () => new Response("unauthorized", { status: 401 }),
      onRefresh: () => jsonResponse(freshSupabaseSession()),
    });
    const { apiFetch, setSession } = await load();
    setSession(HEALTHY);

    await expect(apiFetch("/api/auth/login", { method: "POST" })).rejects.toBeTruthy();

    expect(counts.refresh).toBe(0);
  });
});
