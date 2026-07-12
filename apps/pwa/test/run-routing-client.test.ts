/**
 * Run-routing client — wire contracts against a mocked fetch
 * (lib/api/run-routing.ts → backend /api/v1/run-routing).
 *
 *  - listRunRoutingRules:  GET /api/v1/run-routing
 *  - createRunRoutingRule: POST /api/v1/run-routing — NL surface sends
 *                          {name, source_text, target}; a 422 exposes the
 *                          backend `detail` via ApiError.detail
 *  - updateRunRoutingRule: PATCH /api/v1/run-routing/{id} {source_text?, target?}
 *  - deleteRunRoutingRule: DELETE /api/v1/run-routing/{id} → 204 void
 */

import { ApiError } from "@/lib/api/client";
import {
  createRunRoutingRule,
  deleteRunRoutingRule,
  listRunRoutingRules,
  updateRunRoutingRule,
} from "@/lib/api/run-routing";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const RULE = {
  id: "11111111-1111-1111-1111-111111111111",
  workspace_id: "22222222-2222-2222-2222-222222222222",
  name: "복잡한 작업",
  caller_id: null,
  source_text: "복잡한 작업",
  priority: 10,
  is_default: false,
  target: "opus",
  conditions: [],
  is_active: true,
  created_at: "2026-07-11T00:00:00Z",
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

describe("run-routing client", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("listRunRoutingRules GETs /api/v1/run-routing", async () => {
    const fetchMock = okFetch([RULE]);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await listRunRoutingRules();

    expect(res).toEqual([RULE]);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/run-routing");
    expect((init.method ?? "GET").toUpperCase()).toBe("GET");
  });

  it("createRunRoutingRule POSTs {name, source_text, target}, drops structured fields", async () => {
    const fetchMock = okFetch(RULE, 201);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await createRunRoutingRule({
      name: "복잡한 작업",
      source_text: "복잡한 작업",
      target: "opus",
    });

    expect(res).toEqual(RULE);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/run-routing");
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({ name: "복잡한 작업", source_text: "복잡한 작업", target: "opus" });
    // The rejected structured fields are NOT on the wire.
    expect("caller_id" in body).toBe(false);
    expect("is_default" in body).toBe(false);
    expect("conditions" in body).toBe(false);
  });

  it("createRunRoutingRule exposes the backend 422 detail via ApiError.detail", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ detail: "could not interpret the condition" }), {
          status: 422,
          headers: { "Content-Type": "application/json" },
        }),
    ) as unknown as typeof fetch;

    await expect(
      createRunRoutingRule({ name: "asdf", source_text: "asdf", target: "opus" }),
    ).rejects.toMatchObject({ status: 422, detail: "could not interpret the condition" });
  });

  it("updateRunRoutingRule PATCHes /api/v1/run-routing/{id} with {source_text, target}", async () => {
    const fetchMock = okFetch({ ...RULE, target: "sonnet" });
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await updateRunRoutingRule(RULE.id, {
      source_text: "한국어 요청",
      target: "sonnet",
    });

    expect(res.target).toBe("sonnet");
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`/api/v1/run-routing/${RULE.id}`);
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({
      source_text: "한국어 요청",
      target: "sonnet",
    });
  });

  it("deleteRunRoutingRule DELETEs /api/v1/run-routing/{id} and resolves void", async () => {
    const fetchMock = okFetch(null, 204);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await deleteRunRoutingRule(RULE.id);

    expect(res).toBeUndefined();
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`/api/v1/run-routing/${RULE.id}`);
    expect(init.method).toBe("DELETE");
  });

  it("surfaces an ApiError on a non-ok list read", async () => {
    global.fetch = vi.fn(
      async () => new Response("forbidden", { status: 403 }),
    ) as unknown as typeof fetch;

    await expect(listRunRoutingRules()).rejects.toBeInstanceOf(ApiError);
  });
});
