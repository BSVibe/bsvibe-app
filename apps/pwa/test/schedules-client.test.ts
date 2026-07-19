/**
 * Schedules client — wire contracts against a mocked fetch
 * (lib/api/schedules.ts → backend /api/v1/schedules).
 *
 *  - getSchedules:        GET    /api/v1/schedules
 *  - createSchedule:      POST   /api/v1/schedules with {kind, text, cron_expr}
 *  - deleteSchedule:      DELETE /api/v1/schedules/{id}  (204 → void)
 *  - setScheduleEnabled:  PATCH  /api/v1/schedules/{id}  with {enabled}
 */

import { ApiError } from "@/lib/api/client";
import {
  createSchedule,
  deleteSchedule,
  getSchedules,
  setScheduleEnabled,
} from "@/lib/api/schedules";
import type { Schedule } from "@/lib/api/schedules";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const SCHEDULE: Schedule = {
  id: "sched-1",
  kind: "instruction",
  text: "매주 월요일 시장조사 요약해줘",
  cron_expr: "0 9 * * 1",
  product_id: null,
  title: "주간 시장조사",
  next_run_at: "2026-07-20T00:00:00Z",
  last_fired_at: null,
  enabled: true,
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

describe("schedules client", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("getSchedules GETs /api/v1/schedules", async () => {
    const fetchMock = okFetch([SCHEDULE]);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await getSchedules();

    expect(res).toEqual([SCHEDULE]);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/schedules");
    expect((init.method ?? "GET").toUpperCase()).toBe("GET");
  });

  it("createSchedule POSTs {kind, text, cron_expr} (instruction kind only)", async () => {
    const fetchMock = okFetch(SCHEDULE, 201);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await createSchedule({
      text: "매주 월요일 시장조사 요약해줘",
      cron_expr: "0 9 * * 1",
      title: "주간 시장조사",
    });

    expect(res).toEqual(SCHEDULE);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/schedules");
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body as string);
    expect(body).toMatchObject({
      kind: "instruction",
      text: "매주 월요일 시장조사 요약해줘",
      cron_expr: "0 9 * * 1",
      title: "주간 시장조사",
    });
  });

  it("createSchedule omits an empty title (never sends title:'')", async () => {
    const fetchMock = okFetch(SCHEDULE, 201);
    global.fetch = fetchMock as unknown as typeof fetch;

    await createSchedule({ text: "hi", cron_expr: "0 * * * *", title: "" });

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body).not.toHaveProperty("title");
    expect(body.kind).toBe("instruction");
  });

  it("deleteSchedule DELETEs /api/v1/schedules/{id} (204 → void)", async () => {
    const fetchMock = okFetch(null, 204);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await deleteSchedule("sched-1");

    expect(res).toBeUndefined();
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/schedules/sched-1");
    expect(init.method).toBe("DELETE");
  });

  it("setScheduleEnabled PATCHes {enabled}", async () => {
    const fetchMock = okFetch({ ...SCHEDULE, enabled: false });
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await setScheduleEnabled("sched-1", false);

    expect(res.enabled).toBe(false);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/schedules/sched-1");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({ enabled: false });
  });

  it("surfaces an ApiError on a non-ok create (e.g. 400 bad cron)", async () => {
    global.fetch = vi.fn(
      async () => new Response("bad cron", { status: 400 }),
    ) as unknown as typeof fetch;

    await expect(createSchedule({ text: "x", cron_expr: "not-a-cron" })).rejects.toBeInstanceOf(
      ApiError,
    );
  });
});
