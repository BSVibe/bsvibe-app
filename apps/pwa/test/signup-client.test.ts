/**
 * Signup client — `POST /api/auth/signup`.
 *
 * The backend answers signup in TWO shapes (PR #1004) and the difference is
 * not an error case, it is the Supabase project's "Confirm email" setting:
 *
 *   • Confirm email OFF → `{ session: {...}, confirmation_required: false }`
 *   • Confirm email ON  → `{ session: null,  confirmation_required: true  }`
 *
 * So `signUp` cannot just persist a session. Persisting on the second shape
 * would sign in someone whose address is not proven yet — and the backend
 * deliberately creates NO user row in that case, so the client must not act
 * as though one exists.
 */

import { signUp } from "@/lib/api/auth";
import { clearSession, getSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION_RESPONSE = {
  access_token: "tok",
  refresh_token: "ref",
  expires_in: 3600,
  token_type: "bearer",
  supabase_user_id: "user-1",
  email: "new@bsvibe.dev",
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubOrigin(origin: string) {
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { origin, pathname: "/signup" } as unknown as Location,
  });
}

describe("signUp", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    // `getSession()` reads a MODULE-LEVEL variable, not localStorage — so
    // clearing storage alone leaves the previous test's session in place and
    // "nobody was signed in" assertions pass for the wrong reason.
    clearSession();
    localStorage.clear();
    sessionStorage.clear();
    stubOrigin("http://localhost:3700");
  });

  afterEach(() => {
    vi.restoreAllMocks();
    clearSession();
    localStorage.clear();
    sessionStorage.clear();
  });

  it("posts email + password with a same-origin confirmation redirect", async () => {
    const calls: Array<{ url: string; body: unknown }> = [];
    global.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
      if (url.includes("/api/auth/signup")) {
        return jsonResponse({ session: null, confirmation_required: true });
      }
      return jsonResponse({}, 404);
    }) as unknown as typeof fetch;

    await signUp("new@bsvibe.dev", "pw-12345678");

    const call = calls.find((c) => c.url.includes("/api/auth/signup"));
    expect(call).toBeTruthy();
    expect(call?.body).toEqual({
      email: "new@bsvibe.dev",
      password: "pw-12345678",
      // The backend validates this against its CORS allow-list, so it must be
      // our own origin — never a caller-supplied URL.
      redirect_to: "http://localhost:3700/login",
    });
  });

  it("signs the user in when the backend returns a live session", async () => {
    global.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/auth/signup")) {
        return jsonResponse({ session: SESSION_RESPONSE, confirmation_required: false });
      }
      // `persistSupabaseSession` discovers the personal account afterwards.
      return jsonResponse({ id: "acct-1" });
    }) as unknown as typeof fetch;

    const result = await signUp("new@bsvibe.dev", "pw-12345678");

    expect(result.confirmationRequired).toBe(false);
    expect(getSession()?.accessToken).toBe("tok");
  });

  it("does NOT sign anyone in while a confirmation is pending", async () => {
    global.fetch = vi.fn(async () =>
      jsonResponse({ session: null, confirmation_required: true }),
    ) as unknown as typeof fetch;

    const result = await signUp("pending@bsvibe.dev", "pw-12345678");

    expect(result.confirmationRequired).toBe(true);
    expect(getSession()).toBeNull();
  });

  it("propagates a refusal so the page can show it (signups disabled → 403)", async () => {
    global.fetch = vi.fn(async () =>
      jsonResponse({ detail: "signup is not available for this instance" }, 403),
    ) as unknown as typeof fetch;

    await expect(signUp("nope@bsvibe.dev", "pw-12345678")).rejects.toBeTruthy();
    expect(getSession()).toBeNull();
  });
});
