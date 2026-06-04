/**
 * OAuth consent API client — wire contracts against a mocked fetch
 * (lib/api/oauth.ts → backend /api/oauth/* — public client lookup +
 * consent commit POST).
 *
 * The POST contract is the load-bearing one: the PWA consent screen
 * needs the JSON `{redirect_to}` shape (a cross-origin fetch can't
 * follow a 302 to the OAuth client's loopback callback), so we MUST
 * send `Accept: application/json`.
 */

import { ApiError } from "@/lib/api/client";
import { getOAuthClientByClientId, postOAuthAuthorize } from "@/lib/api/oauth";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const CLIENT_PUBLIC = {
  client_id: "dcr-abc123",
  client_name: "Claude Code",
  client_type: "public",
  redirect_uris: ["http://127.0.0.1/callback"],
  allowed_scopes: ["mcp:read", "mcp:write"],
};

beforeEach(() => {
  setSession(SESSION);
});

afterEach(() => {
  clearSession();
  vi.restoreAllMocks();
});

describe("oauth consent API", () => {
  it("getOAuthClientByClientId GETs the public client lookup endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(CLIENT_PUBLIC), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const row = await getOAuthClientByClientId("dcr-abc123");
    expect(row).toEqual(CLIENT_PUBLIC);
    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/oauth/clients/by-client-id/dcr-abc123");
  });

  it("getOAuthClientByClientId surfaces a 404 as ApiError", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "unknown client" }), {
        status: 404,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await expect(getOAuthClientByClientId("dcr-nope")).rejects.toBeInstanceOf(ApiError);
  });

  it("postOAuthAuthorize sends form body with Accept: application/json", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ redirect_to: "http://127.0.0.1:49921/callback?code=abc&state=s" }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const out = await postOAuthAuthorize(
      {
        response_type: "code",
        client_id: "dcr-abc123",
        redirect_uri: "http://127.0.0.1:49921/callback",
        scope: "mcp:read mcp:write",
        state: "s",
        code_challenge: "cc",
        code_challenge_method: "S256",
      },
      "approve",
    );
    expect(out).toEqual({
      redirect_to: "http://127.0.0.1:49921/callback?code=abc&state=s",
    });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/oauth/authorize");
    expect((init as RequestInit).method).toBe("POST");
    const headers = new Headers((init as RequestInit).headers);
    expect(headers.get("Accept")).toBe("application/json");
    expect(headers.get("Content-Type")).toBe("application/x-www-form-urlencoded");
    expect(headers.get("Authorization")).toBe("Bearer tok");
    // Form body MUST carry action + every original OAuth param.
    const body = new URLSearchParams(String((init as RequestInit).body));
    expect(body.get("action")).toBe("approve");
    expect(body.get("response_type")).toBe("code");
    expect(body.get("client_id")).toBe("dcr-abc123");
    expect(body.get("scope")).toBe("mcp:read mcp:write");
    expect(body.get("state")).toBe("s");
    expect(body.get("code_challenge")).toBe("cc");
  });

  it("postOAuthAuthorize sends action=deny when caller picks Cancel", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          redirect_to: "http://127.0.0.1:49921/callback?error=access_denied&state=s",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const out = await postOAuthAuthorize(
      {
        response_type: "code",
        client_id: "dcr-abc123",
        redirect_uri: "http://127.0.0.1:49921/callback",
        state: "s",
        code_challenge: "cc",
        code_challenge_method: "S256",
      },
      "deny",
    );
    expect(out.redirect_to).toContain("error=access_denied");
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const body = new URLSearchParams(String(init.body));
    expect(body.get("action")).toBe("deny");
  });
});
