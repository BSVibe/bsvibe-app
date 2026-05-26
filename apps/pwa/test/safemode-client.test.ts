/**
 * safemode.ts client — approve / deny POSTs against a mocked fetch. Asserts the
 * wire contract: approve has NO body, deny carries `{ reason }` (the backend
 * SafeModeDenyRequest is extra=forbid), both return SafeModeActionResponse.
 */

import { ApiError } from "@/lib/api/client";
import {
  approveSafeModeItem,
  approveSafeModeRun,
  denySafeModeItem,
  listSafeModeQueueByRun,
} from "@/lib/api/safemode";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const APPROVED = { item_id: "sm-1", status: "approved", dispatched: true };
const DENIED = { item_id: "sm-1", status: "denied", dispatched: false };

describe("safemode client (approve / deny)", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("approve POSTs /api/v1/safemode/{id}/approve with no body", async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify(APPROVED), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await approveSafeModeItem("sm-1");

    expect(res).toEqual(APPROVED);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/safemode/sm-1/approve");
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
  });

  it("deny POSTs /api/v1/safemode/{id}/deny with a { reason } body", async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify(DENIED), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await denySafeModeItem("sm-1");

    expect(res).toEqual(DENIED);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/safemode/sm-1/deny");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ reason: "" });
  });

  it("deny forwards an explicit reason", async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify(DENIED), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    await denySafeModeItem("sm-1", "not now");
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ reason: "not now" });
  });

  it("surfaces an ApiError on a non-ok response", async () => {
    global.fetch = vi.fn(
      async () => new Response("conflict", { status: 409 }),
    ) as unknown as typeof fetch;

    await expect(approveSafeModeItem("sm-1")).rejects.toBeInstanceOf(ApiError);
  });

  // ─────────────────────────────────────────────────────────────────────
  // B12a — per-Run grouping + bulk approve
  // ─────────────────────────────────────────────────────────────────────

  it("listSafeModeQueueByRun GETs /api/v1/safemode/queue/by-run", async () => {
    const GROUPS = [
      {
        run_id: "run-9",
        items: [
          {
            id: "sm-1",
            workspace_id: "ws-1",
            deliverable_id: "del-1",
            run_id: "run-9",
            status: "pending",
            compensation_tier: null,
            expires_at: "2026-05-24T00:00:00Z",
            extension_count: 0,
            created_at: "2026-05-23T12:00:00Z",
          },
        ],
      },
    ];
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify(GROUPS), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await listSafeModeQueueByRun();
    expect(res).toEqual(GROUPS);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit?];
    expect(url).toBe("/api/v1/safemode/queue/by-run");
    expect(init?.method ?? "GET").toBe("GET");
  });

  it("approveSafeModeRun POSTs /api/v1/safemode/runs/{runId}/approve with no body", async () => {
    const APPROVED_RUN = { run_id: "run-9", approved_count: 3, dispatched_count: 3 };
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify(APPROVED_RUN), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await approveSafeModeRun("run-9");
    expect(res).toEqual(APPROVED_RUN);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/safemode/runs/run-9/approve");
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
  });

  it("approveSafeModeRun surfaces an ApiError on 404", async () => {
    global.fetch = vi.fn(
      async () => new Response("not found", { status: 404 }),
    ) as unknown as typeof fetch;

    await expect(approveSafeModeRun("run-x")).rejects.toBeInstanceOf(ApiError);
  });
});
