/**
 * TrustPanel (components/products/TrustPanel.tsx).
 *
 * L3 Inside trust strip — the calm four-line summary on `/products/[slug]`.
 * Verifies:
 *  - Renders four labelled rows (Trend / Touch time / Deposits / Contract).
 *  - When `contract_strength.is_steady === true`, line 4 reads "steady ✓"
 *    in plain ink — no amber class.
 *  - When `contract_strength.is_steady === false`, line 4 carries the
 *    `trust-panel__line--amber` modifier (the only colour treatment, per
 *    design Q5) — and other lines DON'T.
 *  - On a fetch failure, renders a calm one-line error, not a blank.
 *  - No chart / table / sparkline DOM is present (design §4.3 calm shape).
 */

import TrustPanel from "@/components/products/TrustPanel";
import type { ProductTrustResponse } from "@/lib/api/trust.types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "u1",
  expiresAt: Date.now() + 3_600_000,
};

const PRODUCT_ID = "11111111-1111-1111-1111-111111111111";

function trust(over: Partial<ProductTrustResponse> = {}): ProductTrustResponse {
  return {
    product_id: PRODUCT_ID,
    touch_time: {
      total_touch_time_hours: 12,
      decisions_resolved_count: 3,
      decisions_pending_count: 0,
      window_days: 14,
    },
    deposit_rate: { deposit_count: 7, slope_per_day: 1.2, window_days: 14 },
    trend_arrow: { glyph: "↗", reason: "touch ÷ deposits falling — trust rising" },
    contract_strength: { is_steady: true, amber_reason: null },
    ...over,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("TrustPanel", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the calm four-line summary on a steady, rising product", async () => {
    global.fetch = vi.fn(async () => jsonResponse(trust())) as unknown as typeof fetch;

    render(<TrustPanel productId={PRODUCT_ID} />);

    // Heading + four rows.
    await screen.findByRole("heading", { name: /trust/i });
    // Trend line — touch time dropping (rising glyph).
    expect(screen.getByText(/Touch time is dropping/i)).toBeInTheDocument();
    // Touch time line — 12 hours / 14 days.
    expect(screen.getByText(/12 hours of human review/i)).toBeInTheDocument();
    expect(screen.getByText(/14 days/i)).toBeInTheDocument();
    // Deposits line.
    expect(screen.getByText(/7 verified knowledge points/i)).toBeInTheDocument();
    // Contract line — steady, no amber.
    expect(screen.getByText(/Contract strength steady/)).toBeInTheDocument();
  });

  it("applies the amber modifier ONLY to line 4 when contract is not steady", async () => {
    global.fetch = vi.fn(async () =>
      jsonResponse(
        trust({
          contract_strength: {
            is_steady: false,
            amber_reason: "verified_share rising while judge_checks falling",
          },
        }),
      ),
    ) as unknown as typeof fetch;

    const { container } = render(<TrustPanel productId={PRODUCT_ID} />);

    await screen.findByText(/Contract amber/i);
    // Exactly one .trust-panel__line--amber row.
    const ambered = container.querySelectorAll(".trust-panel__line--amber");
    expect(ambered).toHaveLength(1);
    // That row is the contract row, not the trend / touch / deposit rows.
    expect(ambered[0]?.className).toMatch(/trust-panel__line--contract/);
  });

  it("renders a calm one-line error on fetch failure (no blank)", async () => {
    global.fetch = vi.fn(async () => jsonResponse("boom", 500)) as unknown as typeof fetch;

    render(<TrustPanel productId={PRODUCT_ID} />);

    await waitFor(() => {
      expect(screen.getByText(/Couldn.t read the trust signals/i)).toBeInTheDocument();
    });
  });

  it("contains no charts, tables, or sparkline DOM (design §4.3 calm)", async () => {
    global.fetch = vi.fn(async () => jsonResponse(trust())) as unknown as typeof fetch;
    const { container } = render(<TrustPanel productId={PRODUCT_ID} />);
    await screen.findByRole("heading", { name: /trust/i });
    expect(container.querySelector("canvas")).toBeNull();
    expect(container.querySelector("svg")).toBeNull();
    expect(container.querySelector("table")).toBeNull();
    expect(container.querySelector("progress")).toBeNull();
  });

  it("hits the per-product trust endpoint with the given product id", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(trust()));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<TrustPanel productId={PRODUCT_ID} />);

    await screen.findByRole("heading", { name: /trust/i });
    const called = (fetchMock.mock.calls as unknown as Array<[string, RequestInit?]>).some(
      ([url]) => typeof url === "string" && url.endsWith(`/api/v1/inside/trust/${PRODUCT_ID}`),
    );
    expect(called).toBe(true);
  });
});
