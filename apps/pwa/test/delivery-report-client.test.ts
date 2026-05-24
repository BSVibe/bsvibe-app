/**
 * Delivery-report API client — REAL `GET /api/v1/deliverables/{id}/report`
 * (backend/api/v1/deliverables.py). Asserts the client hits the report path
 * and returns the deliverable + verification rows verbatim.
 */

import { getDeliverableReport } from "@/lib/api/deliverables";
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

describe("getDeliverableReport", () => {
  it("requests /api/v1/deliverables/{id}/report and returns the body verbatim", async () => {
    const body = {
      deliverable: {
        id: "d1",
        run_id: "r1",
        workspace_id: "ws-1",
        deliverable_type: "pr",
        summary: "Add getRelatedPosts",
        artifact_refs: ["src/posts.ts"],
        artifact_uri: "https://github.com/acme/repo/pull/15",
        diff_url: "https://github.com/acme/repo/commit/abc",
        created_at: NOW,
      },
      verifications: [
        {
          id: "v1",
          outcome: "passed",
          contract: { checks: [{ kind: "command", command: "pytest -q", rationale: "tests" }] },
          result: { checks: [{ passed: true }] },
          created_at: NOW,
        },
      ],
    };
    const fetchSpy = vi.fn(
      async (_input: RequestInfo | URL) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    global.fetch = fetchSpy as unknown as typeof fetch;

    const result = await getDeliverableReport("d1");

    expect(result).toEqual(body);
    const url = String(fetchSpy.mock.calls[0]?.[0]);
    expect(url).toContain("/api/v1/deliverables/d1/report");
  });
});
