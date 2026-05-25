/**
 * Auth client — social sign-in (PKCE) start + completion, and password reset.
 *
 * `startOAuth` derives a PKCE verifier/challenge, stashes the verifier in
 * sessionStorage, asks the backend for the GoTrue authorize URL, and redirects.
 * `completeOAuth` reads the stashed verifier back and exchanges the `?code=`.
 * `requestPasswordReset` posts the recover request with a same-origin redirect.
 */

import { completeOAuth, requestPasswordReset, startOAuth } from "@/lib/api/auth";
import { getSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const OAUTH_AUTHORIZE_RESPONSE = {
  authorize_url: "https://proj.supabase.co/auth/v1/authorize?provider=google&code_challenge=abc",
};

const SESSION_RESPONSE = {
  access_token: "tok",
  refresh_token: "ref",
  expires_in: 3600,
  token_type: "bearer",
  supabase_user_id: "user-1",
  email: "founder@bsvibe.dev",
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubLocationOrigin(origin: string) {
  const assign = vi.fn();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { origin, pathname: "/login", assign } as unknown as Location,
  });
  return assign;
}

describe("social sign-in (PKCE) + password reset", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    sessionStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    sessionStorage.clear();
  });

  it("startOAuth stashes a PKCE verifier, posts a challenge, and redirects", async () => {
    const assign = stubLocationOrigin("http://localhost:3700");
    const calls: Array<{ url: string; body: unknown }> = [];
    global.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
      if (url.includes("/api/auth/oauth/google/authorize")) {
        return jsonResponse(OAUTH_AUTHORIZE_RESPONSE);
      }
      return jsonResponse({}, 404);
    }) as unknown as typeof fetch;

    await startOAuth("google");

    const verifier = sessionStorage.getItem("bsvibe.pkce_verifier");
    expect(verifier).toBeTruthy();

    const authorizeCall = calls.find((c) => c.url.includes("/authorize"));
    expect(authorizeCall).toBeTruthy();
    const body = authorizeCall?.body as { code_challenge: string; redirect_to: string };
    expect(body.redirect_to).toBe("http://localhost:3700/auth/callback");
    expect(body.code_challenge).toBeTruthy();
    // The challenge is the SHA-256 hash of the verifier, NOT the verifier itself.
    expect(body.code_challenge).not.toBe(verifier);

    expect(assign).toHaveBeenCalledWith(OAUTH_AUTHORIZE_RESPONSE.authorize_url);
  });

  it("completeOAuth exchanges the code with the stashed verifier and persists the session", async () => {
    stubLocationOrigin("http://localhost:3700");
    sessionStorage.setItem("bsvibe.pkce_verifier", "stashed-verifier");
    const calls: Array<{ url: string; body: unknown }> = [];
    global.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
      if (url.includes("/api/auth/oauth/google/callback")) return jsonResponse(SESSION_RESPONSE);
      if (url.includes("/api/v1/account")) return jsonResponse({ id: "acct-1" });
      return jsonResponse({}, 404);
    }) as unknown as typeof fetch;

    await completeOAuth("google", "the-code");

    const callbackCall = calls.find((c) => c.url.includes("/callback"));
    expect((callbackCall?.body as { code: string }).code).toBe("the-code");
    expect((callbackCall?.body as { code_verifier: string }).code_verifier).toBe(
      "stashed-verifier",
    );

    const session = getSession();
    expect(session?.userId).toBe("user-1");
    // The single-use verifier is cleared once consumed.
    expect(sessionStorage.getItem("bsvibe.pkce_verifier")).toBeNull();
  });

  it("requestPasswordReset posts the email with a same-origin reset redirect", async () => {
    stubLocationOrigin("http://localhost:3700");
    const calls: Array<{ url: string; body: unknown }> = [];
    global.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
      return new Response(null, { status: 204 });
    }) as unknown as typeof fetch;

    await requestPasswordReset("founder@bsvibe.dev");

    const call = calls.find((c) => c.url.includes("/api/auth/password/reset"));
    expect(call).toBeTruthy();
    expect(call?.body).toEqual({
      email: "founder@bsvibe.dev",
      redirect_to: "http://localhost:3700/reset-password",
    });
  });
});
