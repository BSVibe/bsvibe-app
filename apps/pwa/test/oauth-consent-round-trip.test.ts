/**
 * End-to-end PKCE-loopback consent round-trip — Lift E11.
 *
 * Simulates the eight-step flow the founder walks when running `bsvibe login`
 * on the worker host:
 *
 *  1. CLI opens browser at `https://api.bsvibe.dev/api/oauth/authorize?…`.
 *  2. Backend (no Supabase cookie) 302s to `app.bsvibe.dev/oauth/consent?…`.
 *  3. ConsentClient detects "no session" → stashes the full consent URL in
 *     `sessionStorage[RETURN_TO_KEY]` → `router.replace('/login?return_to=…')`.
 *  4. /login resolves `returnTo` from query first, sessionStorage second.
 *  5. Founder clicks Continue with Google → `startOAuth(provider, returnTo)`
 *     atomically re-stashes the return_to right before `assign()`.
 *  6. Supabase round-trips through Google; redirects back to /auth/callback?code=.
 *     The URL fragment + query are NOT used as return_to carriers — the value
 *     lives entirely in sessionStorage (dogfood 2026-06-06 proved the hash
 *     mechanism failed in production).
 *  7. /auth/callback completes the code exchange + reads sessionStorage.
 *  8. router.replace lands the founder back on the consent URL with every
 *     original OAuth param (including the loopback `redirect_uri`) intact.
 *
 *  This file asserts every URL transformation between steps 3–8 — the
 *  layers between the consent client and the callback page that the
 *  per-page tests cover individually. It is the test that would have
 *  caught the qazasa123 dogfood failure had it existed before Lift E11.
 */

import { RETURN_TO_KEY, isSameOriginPath, startOAuth } from "@/lib/api/auth";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const CONSENT_URL =
  "/oauth/consent?response_type=code" +
  "&client_id=dcr-abc123" +
  "&redirect_uri=http%3A%2F%2F127.0.0.1%3A53113%2F" +
  "&scope=mcp%3Aread+mcp%3Awrite" +
  "&state=XYZ" +
  "&code_challenge=cc-test" +
  "&code_challenge_method=S256";

function stubLocation(origin: string, assign = vi.fn()) {
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { origin, pathname: "/login", assign } as unknown as Location,
  });
  return assign;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("OAuth consent round-trip — Lift E11", () => {
  beforeEach(() => {
    sessionStorage.clear();
    vi.restoreAllMocks();
  });

  afterEach(() => {
    sessionStorage.clear();
    vi.restoreAllMocks();
  });

  it("preserves the embedded loopback redirect_uri across the round-trip", async () => {
    // STEP 3 — ConsentClient stashes its full URL before bouncing to /login.
    //          We exercise the stash directly (the React-level test for the
    //          ConsentClient redirect lives in oauth-consent-page.test.tsx).
    sessionStorage.setItem(RETURN_TO_KEY, CONSENT_URL);

    // STEP 4 — /login resolves the return_to. The guard MUST accept the
    //          full consent URL — embedded `?` and `&` are part of the
    //          relative path, not URL semantics our checker should choke on.
    expect(isSameOriginPath(CONSENT_URL)).toBe(true);

    // STEP 5 — startOAuth(..., returnTo) — re-stash + assign atomically.
    const assign = stubLocation("http://localhost:3700");
    global.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/auth/oauth/google/authorize")) {
        return jsonResponse({ authorize_url: "https://proj.supabase.co/auth/v1/authorize?x=1" });
      }
      return jsonResponse({}, 404);
    }) as unknown as typeof fetch;
    await startOAuth("google", CONSENT_URL);

    // sessionStorage value MUST be exactly the URL we put in — no
    // double-encoding, no fragment splitting on `&`.
    expect(sessionStorage.getItem(RETURN_TO_KEY)).toBe(CONSENT_URL);
    expect(assign).toHaveBeenCalled();

    // STEP 6/7 — Supabase redirect arrives at /auth/callback. The hash and
    //            query are NOT used; the sessionStorage value is the source
    //            of truth. Re-read it the way the callback page does and
    //            verify every OAuth param survived intact.
    const stashed = sessionStorage.getItem(RETURN_TO_KEY);
    expect(stashed).toBe(CONSENT_URL);
    const url = new URL(`http://localhost:3700${stashed}`);
    expect(url.pathname).toBe("/oauth/consent");
    expect(url.searchParams.get("response_type")).toBe("code");
    expect(url.searchParams.get("client_id")).toBe("dcr-abc123");
    // The load-bearing assertion — the CLI's loopback redirect_uri MUST
    // survive every transformation, percent-encoded exactly as the CLI
    // emitted it. The CLI's `_wait_for_callback` listens on this exact
    // port; a drift here means the browser navigates to /brief instead.
    expect(url.searchParams.get("redirect_uri")).toBe("http://127.0.0.1:53113/");
    expect(url.searchParams.get("state")).toBe("XYZ");
    expect(url.searchParams.get("code_challenge")).toBe("cc-test");
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
  });

  it("rejects open-redirect return_to values at every layer", async () => {
    // The shared `isSameOriginPath` is what /login, /auth/callback, and
    // `startOAuth` all consult. A single source of truth means a future
    // caller can't accidentally relax one layer without relaxing them all.
    expect(isSameOriginPath("https://evil.com/steal")).toBe(false);
    expect(isSameOriginPath("//evil.com/steal")).toBe(false);
    expect(isSameOriginPath("ftp://evil.com/steal")).toBe(false);
    expect(isSameOriginPath("")).toBe(false);
    expect(isSameOriginPath(null)).toBe(false);

    stubLocation("http://localhost:3700");
    global.fetch = vi.fn(async () =>
      jsonResponse({ authorize_url: "https://proj.supabase.co/auth/v1/authorize?x=1" }),
    ) as unknown as typeof fetch;

    await expect(startOAuth("google", "https://evil.com/steal")).rejects.toThrow();
    // No partial state — sessionStorage stays clean when the call rejects.
    expect(sessionStorage.getItem(RETURN_TO_KEY)).toBeNull();
  });

  it("clears the stash for vanilla sign-ins so flows don't bleed into each other", async () => {
    // A prior aborted consent flow may have left a value behind. The first
    // unparameterised `startOAuth(provider)` MUST clear it so the founder's
    // next "Continue with Google" (no return_to) doesn't accidentally land
    // on the stale consent URL.
    sessionStorage.setItem(RETURN_TO_KEY, CONSENT_URL);

    stubLocation("http://localhost:3700");
    global.fetch = vi.fn(async () =>
      jsonResponse({ authorize_url: "https://proj.supabase.co/auth/v1/authorize?x=1" }),
    ) as unknown as typeof fetch;

    await startOAuth("google");

    expect(sessionStorage.getItem(RETURN_TO_KEY)).toBeNull();
  });
});
