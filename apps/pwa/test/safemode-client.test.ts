/**
 * safemode.ts client — approve / deny POSTs against a mocked fetch. Asserts the
 * wire contract: approve has NO body, deny carries `{ reason }` (the backend
 * SafeModeDenyRequest is extra=forbid), both return SafeModeActionResponse.
 */

import { ApiError } from "@/lib/api/client";
import { approveSafeModeItem, denySafeModeItem } from "@/lib/api/safemode";
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
});
