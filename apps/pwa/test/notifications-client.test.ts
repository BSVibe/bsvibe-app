/**
 * Notifications client — wire contracts against a mocked fetch
 * (lib/api/notifications.ts → backend /api/v1/notifications/prefs).
 *
 *  - getNotificationPrefs:    GET /api/v1/notifications/prefs
 *  - updateNotificationPrefs: PUT /api/v1/notifications/prefs with the full
 *                             extra=forbid body (matrix + quiet hours)
 */

import { ApiError } from "@/lib/api/client";
import { getNotificationPrefs, updateNotificationPrefs } from "@/lib/api/notifications";
import type { NotificationPrefs } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const PREFS: NotificationPrefs = {
  matrix: {
    needs_you: { in_app: true, email: true, slack: true },
    triggered: { in_app: true, email: true, slack: false },
    shipped: { in_app: true, email: true, slack: false },
    failed: { in_app: true, email: true, slack: false },
    daily_brief: { in_app: false, email: true, slack: false },
  },
  quiet_hours_enabled: false,
  quiet_hours_start: "22:00",
  quiet_hours_end: "08:00",
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

describe("notifications client", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("getNotificationPrefs GETs /api/v1/notifications/prefs", async () => {
    const fetchMock = okFetch(PREFS);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await getNotificationPrefs();

    expect(res).toEqual(PREFS);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/notifications/prefs");
    expect((init.method ?? "GET").toUpperCase()).toBe("GET");
  });

  it("updateNotificationPrefs PUTs the full prefs body", async () => {
    const next: NotificationPrefs = {
      ...PREFS,
      quiet_hours_enabled: true,
      quiet_hours_start: "23:00",
    };
    const fetchMock = okFetch(next);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await updateNotificationPrefs(next);

    expect(res).toEqual(next);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/notifications/prefs");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body as string)).toEqual(next);
  });

  it("surfaces an ApiError on a non-ok read", async () => {
    global.fetch = vi.fn(
      async () => new Response("forbidden", { status: 403 }),
    ) as unknown as typeof fetch;

    await expect(getNotificationPrefs()).rejects.toBeInstanceOf(ApiError);
  });

  it("surfaces an ApiError on a non-ok update (e.g. 422)", async () => {
    global.fetch = vi.fn(
      async () => new Response("unprocessable", { status: 422 }),
    ) as unknown as typeof fetch;

    await expect(updateNotificationPrefs(PREFS)).rejects.toBeInstanceOf(ApiError);
  });
});
