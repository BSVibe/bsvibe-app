/**
 * Knowledge surface clients — wire contracts against a mocked fetch.
 *
 *  - knowledge.ts: listConcepts + listObservations + getKnowledgeGraph. The
 *    lists GET the read-only /api/v1/inside/* endpoints with a `limit` query
 *    (backend defaults 50 / 25); getKnowledgeGraph GETs /api/v1/inside/graph
 *    and parses the { nodes, edges } shape. Mirrors the backend response shapes
 *    1:1. The backend router keeps the `/inside` prefix even though the surface
 *    is now labeled "Knowledge".
 */

import { ApiError } from "@/lib/api/client";
import { getKnowledgeGraph, listConcepts, listObservations } from "@/lib/api/knowledge";
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

describe("knowledge surface clients", () => {
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

  it("getKnowledgeGraph GETs /api/v1/inside/graph and parses { nodes, edges }", async () => {
    const graph = {
      nodes: [
        { id: "a", label: "Auth", kind: "concept", weight: 1 },
        { id: "b", label: "JWKS", kind: "concept", weight: 1 },
      ],
      edges: [{ source: "a", target: "b", type: "relates_to", weight: 0.8 }],
    };
    const fetchMock = okFetch(graph);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await getKnowledgeGraph();

    expect(res).toEqual(graph);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/inside/graph");
    expect((init.method ?? "GET").toUpperCase()).toBe("GET");
  });

  it("getKnowledgeGraph parses the empty/sparse shape", async () => {
    const fetchMock = okFetch({ nodes: [], edges: [] });
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await getKnowledgeGraph();

    expect(res).toEqual({ nodes: [], edges: [] });
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

  it("surfaces an ApiError on a non-ok graph read", async () => {
    global.fetch = vi.fn(
      async () => new Response("boom", { status: 500 }),
    ) as unknown as typeof fetch;

    await expect(getKnowledgeGraph()).rejects.toBeInstanceOf(ApiError);
  });
});
