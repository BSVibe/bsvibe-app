/**
 * deliverables API client — REAL `GET /api/v1/deliverables`. Asserts the client
 * mirrors the backend DeliverableResponse 1:1 and threads `limit` / `run_id`.
 */

import { listDeliverables } from "@/lib/api/deliverables";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const NOW = "2026-05-23T00:00:00Z";

afterEach(() => {
  vi.restoreAllMocks();
});

beforeEach(() => {
  clearSession();
  setSession(SESSION);
});

describe("listDeliverables", () => {
  it("requests /api/v1/deliverables with a limit and returns the rows verbatim", async () => {
    const rows = [
      {
        id: "d1",
        run_id: "r1",
        workspace_id: "ws-1",
        deliverable_type: "pr",
        summary: "Add getRelatedPosts",
        artifact_refs: ["src/posts.ts"],
        artifact_uri: "https://github.com/acme/repo/pull/15",
        created_at: NOW,
      },
    ];
    const fetchSpy = vi.fn(
      async (_input: RequestInfo | URL) =>
        new Response(JSON.stringify(rows), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    global.fetch = fetchSpy as unknown as typeof fetch;

    const result = await listDeliverables(6);

    expect(result).toEqual(rows);
    const url = String(fetchSpy.mock.calls[0]?.[0]);
    expect(url).toContain("/api/v1/deliverables");
    expect(url).toContain("limit=6");
  });

  it("threads an optional run_id filter into the query", async () => {
    const fetchSpy = vi.fn(
      async (_input: RequestInfo | URL) =>
        new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    global.fetch = fetchSpy as unknown as typeof fetch;

    await listDeliverables(10, "run-42");

    const url = String(fetchSpy.mock.calls[0]?.[0]);
    expect(url).toContain("run_id=run-42");
    expect(url).toContain("limit=10");
  });
});
