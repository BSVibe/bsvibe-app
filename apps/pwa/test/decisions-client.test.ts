/**
 * Decisions surface clients — wire contracts against a mocked fetch.
 *
 *  - checkpoints.ts: list + resolve. Resolve POSTs `{ answer }` to
 *    /api/v1/checkpoints/{id}/resolve (backend ResolveRequest is extra=forbid,
 *    answer min_length=1).
 *  - decisions.ts: accept + reject. Both address the proposal by its vault
 *    path, URL-encoded WHOLE for the `:path` route (so `/` becomes `%2F`).
 *    Reject always sends a `{ reason }` body (extra=forbid, optional reason).
 */

import { listCheckpoints, resolveCheckpoint } from "@/lib/api/checkpoints";
import { ApiError } from "@/lib/api/client";
import { acceptProposal, listPendingProposals, rejectProposal } from "@/lib/api/decisions";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const PROPOSAL_PATH = "proposals/merge-concepts/2026-05-23-self-hosting.md";

function okFetch(body: unknown) {
  return vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
  );
}

describe("decisions surface clients", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("listCheckpoints GETs /api/v1/checkpoints", async () => {
    const rows = [
      {
        id: "c1",
        run_id: "r1",
        decision: "ask",
        question: "Which DB?",
        rationale: null,
        created_at: "2026-05-23T00:00:00Z",
      },
    ];
    const fetchMock = okFetch(rows);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await listCheckpoints();

    expect(res).toEqual(rows);
    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/checkpoints");
  });

  it("resolveCheckpoint POSTs /resolve with the { answer } body", async () => {
    const fetchMock = okFetch({
      id: "c1",
      run_id: "r1",
      status: "resolved",
      resolution: "Use Postgres",
      resolved_at: "2026-05-23T00:01:00Z",
      run_status: "open",
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await resolveCheckpoint("c1", "Use Postgres");

    expect(res.run_status).toBe("open");
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/checkpoints/c1/resolve");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ answer: "Use Postgres" });
  });

  it("listPendingProposals GETs the pending slice", async () => {
    const fetchMock = okFetch([]);
    global.fetch = fetchMock as unknown as typeof fetch;

    await listPendingProposals();

    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/decisions?status_filter=pending&limit=50");
  });

  it("acceptProposal POSTs /accept with the URL-encoded vault path, no body", async () => {
    const fetchMock = okFetch({ proposal_path: PROPOSAL_PATH, status: "accepted", results: [] });
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await acceptProposal(PROPOSAL_PATH);

    expect(res.status).toBe("accepted");
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    // Slashes in the path must be percent-encoded so the whole path lands in
    // the single `{proposal_id:path}` segment.
    expect(url).toBe(`/api/v1/decisions/${encodeURIComponent(PROPOSAL_PATH)}/accept`);
    expect(url).toContain("%2F");
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
  });

  it("rejectProposal POSTs /reject with a { reason } body (empty by default)", async () => {
    const fetchMock = okFetch({ proposal_path: PROPOSAL_PATH, status: "rejected", reason: null });
    global.fetch = fetchMock as unknown as typeof fetch;

    await rejectProposal(PROPOSAL_PATH);

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`/api/v1/decisions/${encodeURIComponent(PROPOSAL_PATH)}/reject`);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ reason: "" });
  });

  it("rejectProposal forwards an explicit reason", async () => {
    const fetchMock = okFetch({
      proposal_path: PROPOSAL_PATH,
      status: "rejected",
      reason: "not a dup",
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    await rejectProposal(PROPOSAL_PATH, "not a dup");

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ reason: "not a dup" });
  });

  it("surfaces an ApiError on a non-ok resolve", async () => {
    global.fetch = vi.fn(
      async () => new Response("not found", { status: 404 }),
    ) as unknown as typeof fetch;

    await expect(resolveCheckpoint("missing", "x")).rejects.toBeInstanceOf(ApiError);
  });

  it("surfaces an ApiError on a non-ok accept (e.g. 409 already resolved)", async () => {
    global.fetch = vi.fn(
      async () => new Response("conflict", { status: 409 }),
    ) as unknown as typeof fetch;

    await expect(acceptProposal(PROPOSAL_PATH)).rejects.toBeInstanceOf(ApiError);
  });
});
