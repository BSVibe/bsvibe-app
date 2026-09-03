/**
 * The fail-closed seam above `listPendingDecisions`.
 *
 * Every read INSIDE that aggregation already catches its own ApiError, so the
 * outer `.catch` in `getBrief` cannot be reached through a normal HTTP failure
 * — wire-cutting it (2026-09-03) turned it off with zero tests going red. It is
 * kept anyway, as the seam that holds when a read is added to the aggregation
 * WITHOUT its own catch: without it the whole Brief falls to `placeholder` and
 * the founder gets an error wall instead of their work.
 *
 * So it is pinned here at the seam itself, by making the aggregation reject.
 * The proposition is not "it returns empty" — it is "it reports the list as
 * UNREAD", the same fact the queues inside report.
 */

import { ApiError } from "@/lib/api/client";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api/pending", () => ({
  listPendingDecisions: vi.fn(),
}));

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

function okEmpty() {
  return vi.fn(
    async () =>
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
  ) as unknown as typeof fetch;
}

describe("getBrief — the pending aggregation failing outright", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });
  afterEach(() => vi.restoreAllMocks());

  it("reports the needs-you list UNREAD (not 'nothing pending') and keeps the Brief alive", async () => {
    const { listPendingDecisions } = await import("@/lib/api/pending");
    vi.mocked(listPendingDecisions).mockRejectedValue(new ApiError(500, "boom"));
    global.fetch = okEmpty();

    const { getBrief } = await import("@/lib/api/brief");
    const view = await getBrief();

    expect(view.needsYouIncomplete).toBe(true);
    expect(view.needsYou).toEqual([]);
    // The seam's whole point: one dead surface must not become an error wall.
    expect(view.placeholder).toBe(false);
  });

  it("a non-API error still propagates — the seam degrades reads, it does not swallow bugs", async () => {
    const { listPendingDecisions } = await import("@/lib/api/pending");
    vi.mocked(listPendingDecisions).mockRejectedValue(new RangeError("a real bug"));
    global.fetch = okEmpty();

    const { getBrief } = await import("@/lib/api/brief");
    await expect(getBrief()).rejects.toBeInstanceOf(RangeError);
  });
});
