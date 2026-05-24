/**
 * Product resources client — wire contracts against a mocked fetch
 * (lib/api/resources.ts → backend /api/v1/products/{id}/resources).
 *
 *  - listResources:  GET    /api/v1/products/{id}/resources
 *  - addResource:    POST   /api/v1/products/{id}/resources with the
 *                    extra=forbid body (kind, title, optional url/note — blank
 *                    url/note dropped)
 *  - removeResource: DELETE /api/v1/products/{id}/resources/{rid}
 */

import { ApiError } from "@/lib/api/client";
import { addResource, listResources, removeResource } from "@/lib/api/resources";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const PRODUCT_ID = "11111111-1111-1111-1111-111111111111";

function okFetch(body: unknown, status = 200) {
  return vi.fn(
    async () =>
      new Response(status === 204 ? null : JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
  );
}

describe("product resources client", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("listResources GETs /api/v1/products/{id}/resources", async () => {
    const rows = [
      {
        id: "22222222-2222-2222-2222-222222222222",
        product_id: PRODUCT_ID,
        workspace_id: "ws-1",
        kind: "repo",
        title: "Main repo",
        url: "https://github.com/acme/blog",
        note: null,
        created_at: "2026-05-25T00:00:00Z",
      },
    ];
    const fetchMock = okFetch(rows);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await listResources(PRODUCT_ID);

    expect(res).toEqual(rows);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`/api/v1/products/${PRODUCT_ID}/resources`);
    expect((init.method ?? "GET").toUpperCase()).toBe("GET");
  });

  it("addResource POSTs kind/title/url/note", async () => {
    const fetchMock = okFetch({}, 201);
    global.fetch = fetchMock as unknown as typeof fetch;

    await addResource(PRODUCT_ID, {
      kind: "repo",
      title: "Main repo",
      url: "https://github.com/acme/blog",
      note: "source of truth",
    });

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`/api/v1/products/${PRODUCT_ID}/resources`);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      kind: "repo",
      title: "Main repo",
      url: "https://github.com/acme/blog",
      note: "source of truth",
    });
  });

  it("addResource drops blank url/note (extra=forbid friendly)", async () => {
    const fetchMock = okFetch({}, 201);
    global.fetch = fetchMock as unknown as typeof fetch;

    await addResource(PRODUCT_ID, { kind: "note", title: "Bare", url: "  ", note: "" });

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({ kind: "note", title: "Bare" });
    expect("url" in body).toBe(false);
    expect("note" in body).toBe(false);
  });

  it("removeResource DELETEs /api/v1/products/{id}/resources/{rid}", async () => {
    const rid = "33333333-3333-3333-3333-333333333333";
    const fetchMock = okFetch(null, 204);
    global.fetch = fetchMock as unknown as typeof fetch;

    await removeResource(PRODUCT_ID, rid);

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`/api/v1/products/${PRODUCT_ID}/resources/${rid}`);
    expect(init.method).toBe("DELETE");
  });

  it("surfaces an ApiError on a non-ok add (e.g. 404 wrong product)", async () => {
    global.fetch = vi.fn(
      async () => new Response("not found", { status: 404 }),
    ) as unknown as typeof fetch;

    await expect(addResource(PRODUCT_ID, { kind: "link", title: "x" })).rejects.toBeInstanceOf(
      ApiError,
    );
  });
});
