/**
 * Auth client login flow — login persists the session AND best-effort fetches
 * the personal account id (`/api/v1/account`) to store as `personalAccountId`.
 *
 * Defensive contract: if the account fetch fails the user is still logged in
 * (the backend's require_account_id fallback covers the missing header).
 */

import { login } from "@/lib/api/auth";
import { getSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const LOGIN_RESPONSE = {
  access_token: "tok",
  refresh_token: "ref",
  expires_in: 3600,
  token_type: "bearer",
  supabase_user_id: "user-1",
  email: "founder@bsvibe.dev",
};

const ACCOUNT_RESPONSE = {
  id: "acct-uuid-1",
  workspace_id: "ws-uuid-1",
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("login flow stores personalAccountId", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("stores personalAccountId from getAccount() after login", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/auth/login")) return jsonResponse(LOGIN_RESPONSE);
      if (url.includes("/api/v1/account")) return jsonResponse(ACCOUNT_RESPONSE);
      return jsonResponse({}, 404);
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    await login("founder@bsvibe.dev", "pw");

    const session = getSession();
    expect(session).not.toBeNull();
    expect(session?.userId).toBe("user-1");
    expect(session?.personalAccountId).toBe("acct-uuid-1");
  });

  it("still logs in when getAccount() rejects (defensive)", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/auth/login")) return jsonResponse(LOGIN_RESPONSE);
      if (url.includes("/api/v1/account")) return jsonResponse("boom", 500);
      return jsonResponse({}, 404);
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    await login("founder@bsvibe.dev", "pw");

    const session = getSession();
    expect(session).not.toBeNull();
    expect(session?.userId).toBe("user-1");
    expect(session?.personalAccountId).toBeUndefined();
  });
});
