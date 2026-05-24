/**
 * apiFetch global 401 handling.
 *
 * When an authenticated read returns 401 (the session expired / was revoked),
 * apiFetch clears the session and redirects to /login exactly once — so the
 * shell never lingers on a stale board behind a wall of console 401s. Loop
 * guards: no redirect/clear when the request path is the login/auth endpoint
 * itself (`/api/auth/*`), no-op when there is no session, and no-op when we are
 * already on /login. A non-401 failure (500 / network throw) never redirects.
 *
 * `client.ts` reads `process.env.NEXT_PUBLIC_BACKEND_URL` at module init, so
 * each case stubs env then imports a fresh module via `vi.resetModules()` +
 * dynamic import.
 */

import type { Session } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

/**
 * Load `client` + `session` from the SAME fresh module graph. `client.ts` reads
 * NEXT_PUBLIC_BACKEND_URL at module init, so each case stubs env then imports
 * fresh via `vi.resetModules()`. The session store is module-scoped, so it must
 * be imported from that same graph — importing it statically at the top would
 * mutate a different instance than the one `client` clears on a 401.
 */
async function load() {
  const session = await import("@/lib/auth/session");
  const client = await import("@/lib/api/client");
  return { ...session, ...client };
}

function mock401() {
  const fetchMock = vi.fn(async () => new Response("unauthorized", { status: 401 }));
  global.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

function mock500() {
  const fetchMock = vi.fn(async () => new Response("boom", { status: 500 }));
  global.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

/** Stub window.location with a spy-able assign + a settable pathname. */
function stubLocation(pathname: string) {
  const assign = vi.fn();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { pathname, assign } as unknown as Location,
  });
  return assign;
}

describe("apiFetch 401 handling", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.stubEnv("NEXT_PUBLIC_BACKEND_URL", "");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("on a 401 with a session present clears the session and redirects to /login", async () => {
    const assign = stubLocation("/brief");
    mock401();
    const { apiFetch, ApiError, setSession, getSession } = await load();
    setSession(SESSION);

    await expect(apiFetch("/api/v1/products")).rejects.toBeInstanceOf(ApiError);

    expect(getSession()).toBeNull();
    expect(assign).toHaveBeenCalledWith("/login");
    expect(assign).toHaveBeenCalledTimes(1);
  });

  it("does NOT redirect or clear on a 401 from an /api/auth/* path (loop guard)", async () => {
    const assign = stubLocation("/login");
    mock401();
    const { apiFetch, ApiError, setSession, getSession } = await load();
    setSession(SESSION);

    await expect(apiFetch("/api/auth/login", { method: "POST" })).rejects.toBeInstanceOf(ApiError);

    expect(getSession()).not.toBeNull();
    expect(assign).not.toHaveBeenCalled();
  });

  it("no-ops the redirect when there is no session (already logged out)", async () => {
    const assign = stubLocation("/brief");
    mock401();
    const { apiFetch, ApiError, clearSession } = await load();
    clearSession();

    await expect(apiFetch("/api/v1/products")).rejects.toBeInstanceOf(ApiError);

    expect(assign).not.toHaveBeenCalled();
  });

  it("no-ops the redirect when already on /login (loop guard)", async () => {
    const assign = stubLocation("/login");
    mock401();
    const { apiFetch, setSession } = await load();
    setSession(SESSION);

    await expect(apiFetch("/api/v1/products")).rejects.toBeTruthy();

    // session is cleared, but no second navigation to /login fires.
    expect(assign).not.toHaveBeenCalled();
  });

  it("does NOT redirect on a 500 (transient / server error)", async () => {
    const assign = stubLocation("/brief");
    mock500();
    const { apiFetch, ApiError, setSession, getSession } = await load();
    setSession(SESSION);

    await expect(apiFetch("/api/v1/products")).rejects.toBeInstanceOf(ApiError);

    expect(getSession()).not.toBeNull();
    expect(assign).not.toHaveBeenCalled();
  });

  it("does NOT redirect when fetch itself throws (network blip)", async () => {
    const assign = stubLocation("/brief");
    global.fetch = vi.fn(async () => {
      throw new TypeError("network down");
    }) as unknown as typeof fetch;
    const { apiFetch, setSession, getSession } = await load();
    setSession(SESSION);

    await expect(apiFetch("/api/v1/products")).rejects.toBeInstanceOf(TypeError);

    expect(getSession()).not.toBeNull();
    expect(assign).not.toHaveBeenCalled();
  });
});
