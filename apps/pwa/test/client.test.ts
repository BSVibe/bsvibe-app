/**
 * apiFetch base-prefix behavior. The PWA calls the backend directly at
 * NEXT_PUBLIC_BACKEND_URL (cross-origin). When the env var is set, requests go
 * to the absolute `${base}${path}`; when unset, they stay relative (`/api/*`)
 * so the existing fetch-mocked client tests keep matching.
 *
 * Because `client.ts` reads `process.env.NEXT_PUBLIC_BACKEND_URL` at module
 * init, each case stubs the env then imports a fresh module via
 * `vi.resetModules()` + dynamic import.
 */

import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const OK = { ok: true };

function mockFetch() {
  const fetchMock = vi.fn(
    async () =>
      new Response(JSON.stringify(OK), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
  );
  global.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

describe("apiFetch base prefix", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("uses a relative path when NEXT_PUBLIC_BACKEND_URL is unset", async () => {
    vi.stubEnv("NEXT_PUBLIC_BACKEND_URL", "");
    const fetchMock = mockFetch();
    const { apiFetch } = await import("@/lib/api/client");

    await apiFetch("/api/v1/messages");

    const [url] = fetchMock.mock.calls[0] as unknown as [string];
    expect(url).toBe("/api/v1/messages");
  });

  it("uses the absolute backend URL when NEXT_PUBLIC_BACKEND_URL is set", async () => {
    vi.stubEnv("NEXT_PUBLIC_BACKEND_URL", "https://api.bsvibe.dev");
    const fetchMock = mockFetch();
    const { apiFetch } = await import("@/lib/api/client");

    await apiFetch("/api/v1/messages");

    const [url] = fetchMock.mock.calls[0] as unknown as [string];
    expect(url).toBe("https://api.bsvibe.dev/api/v1/messages");
  });

  it("does not double-prefix the /api segment", async () => {
    vi.stubEnv("NEXT_PUBLIC_BACKEND_URL", "https://api.bsvibe.dev");
    const fetchMock = mockFetch();
    const { apiFetch } = await import("@/lib/api/client");

    await apiFetch("/api/v1/brief");

    const [url] = fetchMock.mock.calls[0] as unknown as [string];
    expect(url).toBe("https://api.bsvibe.dev/api/v1/brief");
  });

  it("sets X-BSVibe-Account-Id when the session carries personalAccountId", async () => {
    vi.stubEnv("NEXT_PUBLIC_BACKEND_URL", "");
    setSession({ ...SESSION, personalAccountId: "acct-123" });
    const fetchMock = mockFetch();
    const { apiFetch } = await import("@/lib/api/client");

    await apiFetch("/api/v1/accounts");

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.get("X-BSVibe-Account-Id")).toBe("acct-123");
  });

  it("omits X-BSVibe-Account-Id when the session has no personalAccountId", async () => {
    vi.stubEnv("NEXT_PUBLIC_BACKEND_URL", "");
    setSession(SESSION); // no personalAccountId
    const fetchMock = mockFetch();
    const { apiFetch } = await import("@/lib/api/client");

    await apiFetch("/api/v1/accounts");

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.has("X-BSVibe-Account-Id")).toBe(false);
  });
});
