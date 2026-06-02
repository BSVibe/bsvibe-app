/**
 * ProductsIndex Fleet-glance integration (Lift M4b).
 *
 * Verifies that the products overview page wires the fleet-trust endpoint
 * AND renders the trend-arrow glyph beside each product. Also confirms
 * the design §3.2 invariant: the Fleet card displays glyph + name (+ slug
 * for navigation) — but no `touch_time` / `deposit` numbers slip in.
 */

import ProductsIndex from "@/components/products/ProductsIndex";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/products",
}));

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "u1",
  expiresAt: Date.now() + 3_600_000,
};

const PRODUCT_A = {
  id: "11111111-1111-1111-1111-111111111111",
  workspace_id: "ws-1",
  name: "Related Posts",
  slug: "related-posts",
  repo_url: null,
  created_at: "2026-05-23T00:00:00Z",
  updated_at: "2026-05-23T00:00:00Z",
};
const PRODUCT_B = {
  id: "22222222-2222-2222-2222-222222222222",
  workspace_id: "ws-1",
  name: "Widgets",
  slug: "widgets",
  repo_url: null,
  created_at: "2026-05-22T00:00:00Z",
  updated_at: "2026-05-22T00:00:00Z",
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function routedFetch(map: Record<string, unknown>) {
  return vi.fn(async (url: string) => {
    for (const [needle, body] of Object.entries(map)) {
      if (typeof url === "string" && url.includes(needle)) return jsonResponse(body);
    }
    return jsonResponse([], 404);
  });
}

describe("ProductsIndex trust glyphs (Lift M4b)", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
    try {
      window.sessionStorage.clear();
    } catch {
      /* defensive */
    }
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders a trend-arrow glyph next to each product", async () => {
    global.fetch = routedFetch({
      "/api/v1/products": [PRODUCT_A, PRODUCT_B],
      "/api/v1/inside/trust/fleet": {
        products: [
          { product_id: PRODUCT_A.id, trend_arrow: { glyph: "↗", reason: "rising" } },
          { product_id: PRODUCT_B.id, trend_arrow: { glyph: "↘", reason: "needs attention" } },
        ],
      },
    }) as unknown as typeof fetch;

    render(<ProductsIndex />);

    // Wait for both rows.
    await screen.findByText("Related Posts");
    await waitFor(() => {
      expect(screen.getByRole("img", { name: /trust rising/i })).toHaveTextContent("↗");
      expect(screen.getByRole("img", { name: /trust falling/i })).toHaveTextContent("↘");
    });
  });

  it("never renders raw count numbers on the Fleet card (glyph + name + slug only)", async () => {
    global.fetch = routedFetch({
      "/api/v1/products": [PRODUCT_A],
      "/api/v1/inside/trust/fleet": {
        products: [{ product_id: PRODUCT_A.id, trend_arrow: { glyph: "↗", reason: "rising" } }],
      },
    }) as unknown as typeof fetch;

    render(<ProductsIndex />);

    await screen.findByRole("img", { name: /trust rising/i });
    // The Fleet endpoint deliberately omits counts — even so, the rendered
    // card must contain glyph + name + slug only, never digits.
    const item = screen.getByRole("link", { name: /Related Posts/i });
    // Name + slug + glyph, nothing else with digits.
    expect(item.textContent ?? "").not.toMatch(/\d+/);
  });

  it("degrades calmly when the fleet-trust endpoint fails (no glyph, products still render)", async () => {
    global.fetch = vi.fn(async (url: string) => {
      if (url.includes("/api/v1/products")) return jsonResponse([PRODUCT_A]);
      return jsonResponse("boom", 500);
    }) as unknown as typeof fetch;

    render(<ProductsIndex />);

    await screen.findByText("Related Posts");
    // No glyph rendered, but the product link is still there.
    expect(screen.queryByRole("img", { name: /trust/i })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Related Posts/i })).toBeInTheDocument();
  });

  it("supports all four glyphs in the same Fleet response", async () => {
    const products = ["a", "b", "c", "d"].map((s) => ({
      id: `${s}${s}${s}${s}${s}${s}${s}${s}-${s}${s}${s}${s}-${s}${s}${s}${s}-${s}${s}${s}${s}-${s}${s}${s}${s}${s}${s}${s}${s}${s}${s}${s}${s}`,
      workspace_id: "ws-1",
      name: `Product ${s.toUpperCase()}`,
      slug: `product-${s}`,
      repo_url: null,
      created_at: "2026-05-23T00:00:00Z",
      updated_at: "2026-05-23T00:00:00Z",
    }));
    global.fetch = routedFetch({
      "/api/v1/products": products,
      "/api/v1/inside/trust/fleet": {
        products: [
          { product_id: products[0].id, trend_arrow: { glyph: "↗", reason: "rising" } },
          { product_id: products[1].id, trend_arrow: { glyph: "→", reason: "steady" } },
          { product_id: products[2].id, trend_arrow: { glyph: "↘", reason: "falling" } },
          { product_id: products[3].id, trend_arrow: { glyph: "·", reason: "no activity" } },
        ],
      },
    }) as unknown as typeof fetch;

    render(<ProductsIndex />);

    await waitFor(() => {
      expect(screen.getByRole("img", { name: /trust rising/i })).toHaveTextContent("↗");
      expect(screen.getByRole("img", { name: /trust steady/i })).toHaveTextContent("→");
      expect(screen.getByRole("img", { name: /trust falling/i })).toHaveTextContent("↘");
      expect(screen.getByRole("img", { name: /dormant/i })).toHaveTextContent("·");
    });
  });
});
