/**
 * Routing-rules client — wire contracts against a mocked fetch
 * (lib/api/rules.ts → backend /api/v1/rules).
 *
 *  - listRules:   GET /api/v1/rules
 *  - createRule:  POST /api/v1/rules with the extra=forbid body (drops the
 *                 conditions key entirely when none are given so a catch-all
 *                 rule's wire shape stays minimal)
 *  - deleteRule:  DELETE /api/v1/rules/{id} → 204 void
 */

import { ApiError } from "@/lib/api/client";
import { createRule, deleteRule, listRules } from "@/lib/api/rules";
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
  name: "Substantial work",
  priority: 10,
  target_model: "opencode/plan-builder",
  is_default: false,
  is_active: true,
  conditions: [],
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

describe("rules client", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("listRules GETs /api/v1/rules", async () => {
    const fetchMock = okFetch([RULE]);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await listRules();

    expect(res).toEqual([RULE]);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/rules");
    expect((init.method ?? "GET").toUpperCase()).toBe("GET");
  });

  it("createRule POSTs a minimal catch-all body (no conditions key)", async () => {
    const fetchMock = okFetch(RULE, 201);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await createRule({
      name: "Substantial work",
      target_model: "opencode/plan-builder",
      priority: 10,
      is_default: false,
    });

    expect(res).toEqual(RULE);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/rules");
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({
      name: "Substantial work",
      target_model: "opencode/plan-builder",
      priority: 10,
      is_default: false,
    });
    // No empty conditions array on the wire — the backend defaults it.
    expect("conditions" in body).toBe(false);
  });

  it("createRule includes conditions when given", async () => {
    const fetchMock = okFetch(RULE, 201);
    global.fetch = fetchMock as unknown as typeof fetch;

    await createRule({
      name: "Simple chores",
      target_model: "ollama/qwen3",
      priority: 5,
      is_default: false,
      conditions: [
        { condition_type: "intent", field: "classified_intent", operator: "eq", value: "chore" },
      ],
    });

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.conditions).toEqual([
      { condition_type: "intent", field: "classified_intent", operator: "eq", value: "chore" },
    ]);
  });

  it("deleteRule DELETEs /api/v1/rules/{id} and resolves void", async () => {
    const fetchMock = okFetch(null, 204);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await deleteRule(RULE.id);

    expect(res).toBeUndefined();
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`/api/v1/rules/${RULE.id}`);
    expect(init.method).toBe("DELETE");
  });

  it("surfaces an ApiError on a non-ok list read", async () => {
    global.fetch = vi.fn(
      async () => new Response("forbidden", { status: 403 }),
    ) as unknown as typeof fetch;

    await expect(listRules()).rejects.toBeInstanceOf(ApiError);
  });
});
