/**
 * Run-routing client — wire contracts against a mocked fetch
 * (lib/api/run-routing.ts → backend /api/v1/run-routing).
 *
 *  - listRunRoutingRules:   GET /api/v1/run-routing
 *  - listRunRoutingCallers: GET /api/v1/run-routing/callers
 *  - createRunRoutingRule:  POST /api/v1/run-routing (extra=forbid body; omits
 *                           caller_id when unset, drops conditions when empty)
 *  - deleteRunRoutingRule:  DELETE /api/v1/run-routing/{id} → 204 void
 */

import { ApiError } from "@/lib/api/client";
import {
  compileRunRoutingRules,
  createRunRoutingRule,
  deleteRunRoutingRule,
  listRunRoutingCallers,
  listRunRoutingRules,
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
  name: "design → opus",
  caller_id: "workflow.agent_loop.plan",
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

  it("listRunRoutingCallers GETs /api/v1/run-routing/callers", async () => {
    const callers = [{ caller_id: "workflow.agent_loop.plan", description: "design step" }];
    const fetchMock = okFetch(callers);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await listRunRoutingCallers();

    expect(res).toEqual(callers);
    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/run-routing/callers");
  });

  it("createRunRoutingRule POSTs caller_id + target, drops empty conditions", async () => {
    const fetchMock = okFetch(RULE, 201);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await createRunRoutingRule({
      name: "design → opus",
      caller_id: "workflow.agent_loop.plan",
      target: "opus",
      priority: 10,
      is_default: false,
    });

    expect(res).toEqual(RULE);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/run-routing");
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({
      name: "design → opus",
      caller_id: "workflow.agent_loop.plan",
      target: "opus",
      priority: 10,
      is_default: false,
    });
    expect("conditions" in body).toBe(false);
  });

  it("createRunRoutingRule omits caller_id for a catch-all default", async () => {
    const fetchMock = okFetch(RULE, 201);
    global.fetch = fetchMock as unknown as typeof fetch;

    await createRunRoutingRule({
      name: "default → sonnet",
      caller_id: null,
      target: "sonnet",
      priority: 100,
      is_default: true,
    });

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect("caller_id" in body).toBe(false);
    expect(body.is_default).toBe(true);
  });

  it("compileRunRoutingRules POSTs /api/v1/run-routing/compile with the text", async () => {
    const result = {
      proposals: [{ name: "d", caller_id: null, target: "opus", priority: 10, is_default: true }],
    };
    const fetchMock = okFetch(result);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await compileRunRoutingRules("설계는 opus");

    expect(res).toEqual(result);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/run-routing/compile");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ text: "설계는 opus" });
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
