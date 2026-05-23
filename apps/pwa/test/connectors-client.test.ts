/**
 * Connectors client — wire contracts against a mocked fetch
 * (lib/api/connectors.ts → backend /api/v1/connectors).
 *
 *  - listConnectors:   GET /api/v1/connectors
 *  - createConnector:  POST /api/v1/connectors with the extra=forbid body
 *                      (drops blank external_ref, always sends delivery_config)
 *  - revokeConnector:  DELETE /api/v1/connectors/{id} → 204 void
 */

import { ApiError } from "@/lib/api/client";
import { createConnector, listConnectors, revokeConnector } from "@/lib/api/connectors";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

function okFetch(body: unknown, status = 200) {
  return vi.fn(
    async () =>
      new Response(status === 204 ? null : JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
  );
}

describe("connectors client", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("listConnectors GETs /api/v1/connectors", async () => {
    const rows = [
      {
        id: "11111111-1111-1111-1111-111111111111",
        connector: "github",
        external_ref: "acme/widgets",
        is_active: true,
        created_at: "2026-05-23T00:00:00Z",
        delivery_config: {},
        token_hint: "...wxyz",
      },
    ];
    const fetchMock = okFetch(rows);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await listConnectors();

    expect(res).toEqual(rows);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/connectors");
    expect((init.method ?? "GET").toUpperCase()).toBe("GET");
  });

  it("createConnector POSTs the full body including delivery_config", async () => {
    const created = {
      id: "22222222-2222-2222-2222-222222222222",
      connector: "notion",
      external_ref: "ops-page",
      is_active: true,
      created_at: "2026-05-23T00:00:00Z",
      delivery_config: { parent_page_id: "pp-1" },
      webhook_token: "super-secret-token-xyz",
      webhook_url: "/api/webhooks/notion/super-secret-token-xyz",
    };
    const fetchMock = okFetch(created, 201);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await createConnector({
      connector: "notion",
      signing_secret: "shh",
      external_ref: "ops-page",
      delivery_config: { parent_page_id: "pp-1" },
    });

    expect(res).toEqual(created);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/connectors");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      connector: "notion",
      signing_secret: "shh",
      external_ref: "ops-page",
      delivery_config: { parent_page_id: "pp-1" },
    });
  });

  it("createConnector drops a blank external_ref and defaults delivery_config to {}", async () => {
    const fetchMock = okFetch({}, 201);
    global.fetch = fetchMock as unknown as typeof fetch;

    await createConnector({ connector: "github", signing_secret: "s", external_ref: "  " });

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({ connector: "github", signing_secret: "s", delivery_config: {} });
    expect("external_ref" in body).toBe(false);
  });

  it("revokeConnector DELETEs /api/v1/connectors/{id} and resolves void", async () => {
    const fetchMock = okFetch(null, 204);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await revokeConnector("33333333-3333-3333-3333-333333333333");

    expect(res).toBeUndefined();
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/connectors/33333333-3333-3333-3333-333333333333");
    expect(init.method).toBe("DELETE");
  });

  it("surfaces an ApiError on a non-ok list read", async () => {
    global.fetch = vi.fn(
      async () => new Response("forbidden", { status: 403 }),
    ) as unknown as typeof fetch;

    await expect(listConnectors()).rejects.toBeInstanceOf(ApiError);
  });

  it("surfaces an ApiError on a non-ok create (e.g. 422 unknown connector)", async () => {
    global.fetch = vi.fn(
      async () => new Response("unprocessable", { status: 422 }),
    ) as unknown as typeof fetch;

    await expect(
      createConnector({ connector: "linear", signing_secret: "s" }),
    ).rejects.toBeInstanceOf(ApiError);
  });
});
