/**
 * Workers client — wire contracts against a mocked fetch
 * (lib/api/workers.ts → backend /api/v1/workers).
 *
 *  - listWorkers:   GET    /api/v1/workers
 *  - revokeWorker:  DELETE /api/v1/workers/{id} → 204 void
 *
 * Lift E5 (2026-06-06) — the legacy `mintInstallToken` client is gone; the
 * PWA never sees an install token because registration happens host-side via
 * `bsvibe-worker register` with the host OAuth bearer.
 */

import { ApiError } from "@/lib/api/client";
import { listWorkers, revokeWorker } from "@/lib/api/workers";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
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

describe("workers client", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("listWorkers GETs /api/v1/workers", async () => {
    const rows = [
      {
        id: "11111111-1111-1111-1111-111111111111",
        workspace_id: "ws-1",
        name: "studio-mini",
        labels: ["mac"],
        capabilities: ["claude_code", "codex"],
        status: "online",
        is_active: true,
      },
    ];
    const fetchMock = okFetch(rows);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await listWorkers();

    expect(res).toEqual(rows);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/workers");
    expect((init.method ?? "GET").toUpperCase()).toBe("GET");
  });

  it("revokeWorker DELETEs /api/v1/workers/{id} and resolves void", async () => {
    const fetchMock = okFetch(null, 204);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await revokeWorker("33333333-3333-3333-3333-333333333333");

    expect(res).toBeUndefined();
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/workers/33333333-3333-3333-3333-333333333333");
    expect(init.method).toBe("DELETE");
  });

  it("surfaces an ApiError on a non-ok list read", async () => {
    global.fetch = vi.fn(
      async () => new Response("forbidden", { status: 403 }),
    ) as unknown as typeof fetch;

    await expect(listWorkers()).rejects.toBeInstanceOf(ApiError);
  });
});
