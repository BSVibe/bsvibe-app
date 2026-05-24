/**
 * Products client — wire contracts against a mocked fetch
 * (lib/api/products.ts → backend /api/v1/products).
 *
 *  - listProducts:  GET /api/v1/products
 *  - createProduct: POST /api/v1/products with the extra=forbid body
 *                   (name, slug, optional repo_url — drops blank repo_url)
 */

import { ApiError } from "@/lib/api/client";
import { createProduct, listProducts } from "@/lib/api/products";
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

describe("products client", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("listProducts GETs /api/v1/products", async () => {
    const rows = [
      {
        id: "11111111-1111-1111-1111-111111111111",
        workspace_id: "ws-1",
        name: "Widgets",
        slug: "widgets",
        repo_url: null,
        created_at: "2026-05-23T00:00:00Z",
        updated_at: "2026-05-23T00:00:00Z",
      },
    ];
    const fetchMock = okFetch(rows);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await listProducts();

    expect(res).toEqual(rows);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/products");
    expect((init.method ?? "GET").toUpperCase()).toBe("GET");
  });

  it("createProduct POSTs name/slug/repo_url to /api/v1/products", async () => {
    const created = {
      id: "22222222-2222-2222-2222-222222222222",
      workspace_id: "ws-1",
      name: "Related Posts",
      slug: "related-posts",
      repo_url: "https://github.com/acme/related-posts",
      created_at: "2026-05-23T00:00:00Z",
      updated_at: "2026-05-23T00:00:00Z",
    };
    const fetchMock = okFetch(created, 201);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await createProduct({
      name: "Related Posts",
      slug: "related-posts",
      repo_url: "https://github.com/acme/related-posts",
    });

    expect(res).toEqual(created);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/products");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      name: "Related Posts",
      slug: "related-posts",
      repo_url: "https://github.com/acme/related-posts",
    });
  });

  it("createProduct drops a blank repo_url (extra=forbid friendly)", async () => {
    const fetchMock = okFetch({}, 201);
    global.fetch = fetchMock as unknown as typeof fetch;

    await createProduct({ name: "Widgets", slug: "widgets", repo_url: "  " });

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({ name: "Widgets", slug: "widgets" });
    expect("repo_url" in body).toBe(false);
  });

  it("createProduct omits repo_url when not provided", async () => {
    const fetchMock = okFetch({}, 201);
    global.fetch = fetchMock as unknown as typeof fetch;

    await createProduct({ name: "Widgets", slug: "widgets" });

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({ name: "Widgets", slug: "widgets" });
  });

  it("surfaces an ApiError on a non-ok create (e.g. 409 duplicate slug)", async () => {
    global.fetch = vi.fn(
      async () => new Response("conflict", { status: 409 }),
    ) as unknown as typeof fetch;

    await expect(createProduct({ name: "Widgets", slug: "widgets" })).rejects.toBeInstanceOf(
      ApiError,
    );
  });
});
