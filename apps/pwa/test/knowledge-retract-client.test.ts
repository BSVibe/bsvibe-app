/**
 * Knowledge retract / correct / undo wire contracts (Lift M3b PWA half of the
 * M3a backend surface). Asserts:
 *  - retractNode POSTs `/api/v1/inside/nodes/{node_ref}/retract` with the body
 *    `{ correction_id?, reason? }` and parses the `RetractResponse` shape
 *    (`signal`, `created`, `undo_window_seconds`)
 *  - correctNode POSTs `/api/v1/inside/nodes/{node_ref}/correct` with the body
 *    `{ correction_id?, reason?, corrections? }` and parses the same response
 *    shape
 *  - undoCorrection POSTs `/api/v1/inside/corrections/{id}/undo` and parses
 *    the `UndoCorrectionResponse` (`{correction_id, status}`)
 *  - node_ref `/` literals are preserved (the backend mounts via `:path`)
 *  - non-`/` unsafe characters (spaces / `?`) are percent-encoded
 *  - the Authorization Bearer header rides on the POST (the same `apiFetch`
 *    rules as the GETs)
 *  - non-ok responses surface as `ApiError` with the wire status
 */

import { ApiError } from "@/lib/api/client";
import { correctNode, retractNode, undoCorrection } from "@/lib/api/knowledge";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const RETRACT_RESPONSE = {
  signal: {
    id: "11111111-1111-1111-1111-111111111111",
    workspace_id: "22222222-2222-2222-2222-222222222222",
    actor_id: "33333333-3333-3333-3333-333333333333",
    node_ref: "garden/seedling/rate-limit.md",
    action: "retract",
    issued_at: "2026-05-30T12:00:00Z",
    apply_at: "2026-05-30T12:00:30Z",
    reason: "we changed the rate limit policy",
    source: "ontology_inspect_ui",
  },
  created: true,
  undo_window_seconds: 30,
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

describe("knowledge retract / correct / undo wire", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("retractNode POSTs to the retract endpoint with the JSON body", async () => {
    const fetchMock = okFetch(RETRACT_RESPONSE);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await retractNode("garden/seedling/rate-limit.md", {
      reason: "we changed the rate limit policy",
    });

    expect(res).toEqual(RETRACT_RESPONSE);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/inside/nodes/garden/seedling/rate-limit.md/retract");
    expect((init.method ?? "GET").toUpperCase()).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ reason: "we changed the rate limit policy" }));
    const headers = new Headers(init.headers);
    expect(headers.get("Authorization")).toBe("Bearer tok");
    expect(headers.get("Content-Type")).toBe("application/json");
  });

  it("retractNode preserves `/` literals so the backend `:path` converter matches", async () => {
    const fetchMock = okFetch(RETRACT_RESPONSE);
    global.fetch = fetchMock as unknown as typeof fetch;

    await retractNode("garden/seedling/2026-05-30-foo.md");

    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/inside/nodes/garden/seedling/2026-05-30-foo.md/retract");
  });

  it("retractNode percent-encodes non-`/` unsafe characters in node_ref", async () => {
    const fetchMock = okFetch(RETRACT_RESPONSE);
    global.fetch = fetchMock as unknown as typeof fetch;

    await retractNode("garden/seedling/has space?.md");

    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/inside/nodes/garden/seedling/has%20space%3F.md/retract");
  });

  it("retractNode forwards an optional correction_id (idempotency key)", async () => {
    const fetchMock = okFetch(RETRACT_RESPONSE);
    global.fetch = fetchMock as unknown as typeof fetch;

    await retractNode("garden/seedling/foo.md", {
      correction_id: "ddddddd1-dddd-dddd-dddd-dddddddddddd",
      reason: "typo",
    });

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.body).toBe(
      JSON.stringify({
        correction_id: "ddddddd1-dddd-dddd-dddd-dddddddddddd",
        reason: "typo",
      }),
    );
  });

  it("correctNode POSTs to the correct endpoint with corrections + reason", async () => {
    const fetchMock = okFetch({
      ...RETRACT_RESPONSE,
      signal: { ...RETRACT_RESPONSE.signal, action: "correct" },
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await correctNode("garden/seedling/foo.md", {
      reason: "replace the body",
      corrections: { body: "the new body" },
    });

    expect(res.signal.action).toBe("correct");
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/inside/nodes/garden/seedling/foo.md/correct");
    expect((init.method ?? "GET").toUpperCase()).toBe("POST");
    expect(init.body).toBe(
      JSON.stringify({
        reason: "replace the body",
        corrections: { body: "the new body" },
      }),
    );
  });

  it("undoCorrection POSTs to the undo endpoint and parses the terminal status", async () => {
    const undoBody = {
      correction_id: "11111111-1111-1111-1111-111111111111",
      status: "undone",
    };
    const fetchMock = okFetch(undoBody);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await undoCorrection("11111111-1111-1111-1111-111111111111");

    expect(res).toEqual(undoBody);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/inside/corrections/11111111-1111-1111-1111-111111111111/undo");
    expect((init.method ?? "GET").toUpperCase()).toBe("POST");
  });

  it("undoCorrection surfaces the `expired` terminal status verbatim", async () => {
    global.fetch = okFetch({
      correction_id: "11111111-1111-1111-1111-111111111111",
      status: "expired",
    }) as unknown as typeof fetch;

    const res = await undoCorrection("11111111-1111-1111-1111-111111111111");

    expect(res.status).toBe("expired");
  });

  it("retractNode surfaces an ApiError on a non-ok response", async () => {
    global.fetch = vi.fn(
      async () => new Response("not found", { status: 404 }),
    ) as unknown as typeof fetch;

    await expect(retractNode("garden/seedling/missing.md")).rejects.toBeInstanceOf(ApiError);
  });

  it("undoCorrection surfaces an ApiError on a non-ok response", async () => {
    global.fetch = vi.fn(
      async () => new Response("not found", { status: 404 }),
    ) as unknown as typeof fetch;

    await expect(undoCorrection("11111111-1111-1111-1111-111111111111")).rejects.toBeInstanceOf(
      ApiError,
    );
  });
});
