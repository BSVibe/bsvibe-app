/**
 * Inside surface clients — wire contracts against a mocked fetch.
 *
 *  - inside.ts: listConcepts + listObservations. Both GET the read-only
 *    /api/v1/inside/* lists with a `limit` query (backend defaults 50 / 25).
 *    Mirrors the backend Concept/Observation response shapes 1:1.
 */

import { ApiError } from "@/lib/api/client";
import { listConcepts, listObservations } from "@/lib/api/inside";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

function okFetch(body: unknown) {
  return vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
  );
}

describe("inside surface clients", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("listConcepts GETs /api/v1/inside/concepts with the default limit", async () => {
    const rows = [
      {
        id: "self-hosting",
        name: "Self-hosting",
        summary: "Running services on owned hardware.",
        aliases: ["self host", "selfhosting"],
        alias_count: 2,
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-23T00:00:00Z",
      },
    ];
    const fetchMock = okFetch(rows);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await listConcepts();

    expect(res).toEqual(rows);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/inside/concepts?limit=50");
    expect((init.method ?? "GET").toUpperCase()).toBe("GET");
  });

  it("listConcepts forwards an explicit limit", async () => {
    const fetchMock = okFetch([]);
    global.fetch = fetchMock as unknown as typeof fetch;

    await listConcepts(10);

    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/inside/concepts?limit=10");
  });

  it("listObservations GETs /api/v1/inside/observations with the default limit", async () => {
    const rows = [
      {
        id: "garden/seedling/2026-05-23-related-posts.md",
        title: "Related posts widget",
        excerpt: "Settled on showing 5 items per the founder call.",
        tags: ["frontend", "widget"],
        captured_at: "2026-05-23T00:00:00Z",
      },
    ];
    const fetchMock = okFetch(rows);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await listObservations();

    expect(res).toEqual(rows);
    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/inside/observations?limit=25");
  });

  it("listObservations forwards an explicit limit", async () => {
    const fetchMock = okFetch([]);
    global.fetch = fetchMock as unknown as typeof fetch;

    await listObservations(5);

    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/inside/observations?limit=5");
  });

  it("surfaces an ApiError on a non-ok concepts read", async () => {
    global.fetch = vi.fn(
      async () => new Response("forbidden", { status: 403 }),
    ) as unknown as typeof fetch;

    await expect(listConcepts()).rejects.toBeInstanceOf(ApiError);
  });

  it("surfaces an ApiError on a non-ok observations read", async () => {
    global.fetch = vi.fn(
      async () => new Response("boom", { status: 500 }),
    ) as unknown as typeof fetch;

    await expect(listObservations()).rejects.toBeInstanceOf(ApiError);
  });
});
