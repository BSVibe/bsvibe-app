/**
 * OAuth clients API client — wire contracts against a mocked fetch
 * (lib/api/oauth-clients.ts → backend /api/v1/oauth/clients).
 */

import { ApiError } from "@/lib/api/client";
import { createOAuthClient, deleteOAuthClient, listOAuthClients } from "@/lib/api/oauth-clients";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const CLIENT = {
  id: "11111111-1111-1111-1111-111111111111",
  client_id: "dcr-abc123",
  client_name: "Claude Code",
  client_type: "public",
  redirect_uris: ["http://127.0.0.1/callback"],
  allowed_scopes: ["mcp:read", "mcp:write"],
  created_at: "2026-06-03T00:00:00Z",
  revoked_at: null as string | null,
};

beforeEach(() => {
  setSession(SESSION);
});

afterEach(() => {
  clearSession();
  vi.restoreAllMocks();
});

describe("oauth-clients API", () => {
  it("listOAuthClients GETs /api/v1/oauth/clients with bearer auth", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify([CLIENT]), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const rows = await listOAuthClients();
    expect(rows).toEqual([CLIENT]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/oauth/clients");
    const headers = new Headers((init as RequestInit).headers);
    expect(headers.get("Authorization")).toBe("Bearer tok");
  });

  it("createOAuthClient POSTs the form body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(CLIENT), {
        status: 201,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const row = await createOAuthClient({
      client_name: "Claude Code",
      redirect_uris: ["http://127.0.0.1/callback"],
      allowed_scopes: ["mcp:read"],
    });
    expect(row).toEqual(CLIENT);
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toMatchObject({
      client_name: "Claude Code",
      redirect_uris: ["http://127.0.0.1/callback"],
      allowed_scopes: ["mcp:read"],
    });
  });

  it("deleteOAuthClient DELETEs the encoded client id", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    await deleteOAuthClient("dcr-with/slash");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/oauth/clients/dcr-with%2Fslash");
    expect((init as RequestInit).method).toBe("DELETE");
  });

  it("surfaces backend 400 as ApiError", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "redirect_uri must be https" }), {
        status: 400,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await expect(
      createOAuthClient({
        client_name: "Bad",
        redirect_uris: ["http://evil.com/cb"],
      }),
    ).rejects.toBeInstanceOf(ApiError);
  });
});
