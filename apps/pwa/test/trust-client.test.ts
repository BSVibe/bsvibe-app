/**
 * Trust API client (lib/api/trust.ts) — wire-shape verification.
 *
 * The Fleet glance and Inside trust strip both depend on the wire shapes
 * being 1:1 with the M4a backend Pydantic schemas. These tests pin those
 * shapes so a backend rename / extra field shows up as a failure here
 * before it silently breaks the surfaces.
 */

import { getFleetTrust, getProductTrust } from "@/lib/api/trust";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "u1",
  expiresAt: Date.now() + 3_600_000,
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("trust API client", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("GET /api/v1/inside/trust/fleet returns the list-of-entries shape", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        products: [
          {
            product_id: "11111111-1111-1111-1111-111111111111",
            trend_arrow: { glyph: "↗", reason: "rising" },
          },
          {
            product_id: "22222222-2222-2222-2222-222222222222",
            trend_arrow: { glyph: "·", reason: "no activity in window" },
          },
        ],
      }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await getFleetTrust();
    expect(res.products).toHaveLength(2);
    expect(res.products[0].trend_arrow.glyph).toBe("↗");
    expect(res.products[1].trend_arrow.glyph).toBe("·");

    const [url] = fetchMock.mock.calls[0] as unknown as [string];
    expect(url).toBe("/api/v1/inside/trust/fleet");
  });

  it("GET /api/v1/inside/trust/{id} returns the composite per-product shape", async () => {
    const productId = "11111111-1111-1111-1111-111111111111";
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        product_id: productId,
        touch_time: {
          total_touch_time_hours: 4.5,
          decisions_resolved_count: 2,
          decisions_pending_count: 1,
          window_days: 14,
        },
        deposit_rate: { deposit_count: 3, slope_per_day: 0.3, window_days: 14 },
        trend_arrow: { glyph: "→", reason: "north-star ratio steady" },
        contract_strength: { is_steady: true, amber_reason: null },
      }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await getProductTrust(productId);
    expect(res.product_id).toBe(productId);
    expect(res.touch_time.total_touch_time_hours).toBe(4.5);
    expect(res.deposit_rate.deposit_count).toBe(3);
    expect(res.trend_arrow.glyph).toBe("→");
    expect(res.contract_strength.is_steady).toBe(true);

    const [url] = fetchMock.mock.calls[0] as unknown as [string];
    expect(url).toBe(`/api/v1/inside/trust/${productId}`);
  });
});
