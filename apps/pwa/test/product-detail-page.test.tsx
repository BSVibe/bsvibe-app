/**
 * Product detail surface — the ProductDetail container, driven by a route-aware
 * mocked fetch. Asserts:
 *  - the header renders the product name + plain-language current status
 *  - "Recent runs" lists the product's runs with their plain-language status
 *  - "Shipped" lists the product's delivered artifacts with the verified verdict
 *    + external link
 *  - the calm not-found state for an unknown slug (+ a way back to the Brief)
 *  - a calm inline error (not a blank page) when the core read fails
 *  - the calm empty states (no runs / nothing shipped yet)
 *  - a loading note before the read lands
 */

import ProductDetail from "@/components/products/ProductDetail";
import type { Deliverable, Product, Run } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const NOW = "2026-05-23T00:00:00Z";

const BLOG: Product = {
  id: "p-blog",
  workspace_id: "ws-1",
  name: "Blog",
  slug: "blog",
  repo_url: "https://github.com/acme/blog",
  created_at: NOW,
  updated_at: NOW,
};

const SHIPPED_RUN: Run = {
  id: "run-ship",
  workspace_id: "ws-1",
  product_id: "p-blog",
  request_id: null,
  intent: null,
  status: "shipped",
  created_at: NOW,
  updated_at: NOW,
};

const RUNNING_RUN: Run = { ...SHIPPED_RUN, id: "run-work", status: "running" };

const DELIVERABLE: Deliverable = {
  id: "d1",
  run_id: "run-ship",
  workspace_id: "ws-1",
  deliverable_type: "pr",
  summary: "Add related-posts widget",
  artifact_refs: [],
  artifact_uri: "https://github.com/acme/blog/pull/15",
  // B4: backed by a PASSED VerificationResult → the green "This is verified".
  verified: true,
  created_at: NOW,
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installFetch(opts: {
  products?: () => Product[] | Response;
  runs?: () => Run[] | Response;
  deliverables?: (runId: string) => Deliverable[] | Response;
}) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    // Specific sub-paths under /api/v1/products/{id}/... must be matched
    // BEFORE the bare /products list — order is load-bearing.
    if (url.match(/^\/api\/v1\/products\/[^/]+\/(resources|bindings)/)) {
      return json([]);
    }
    if (url.startsWith("/api/v1/products")) {
      const p = opts.products?.() ?? [BLOG];
      return p instanceof Response ? p : json(p);
    }
    if (url.startsWith("/api/v1/runs")) {
      const r = opts.runs?.() ?? [];
      return r instanceof Response ? r : json(r);
    }
    if (url.startsWith("/api/v1/deliverables")) {
      const runId = new URLSearchParams(url.split("?")[1] ?? "").get("run_id") ?? "";
      const d = opts.deliverables?.(runId) ?? [];
      return d instanceof Response ? d : json(d);
    }
    // M4b: TrustPanel reads /api/v1/inside/trust/{id} on mount. The
    // ProductDetail tests don't care about trust content — just that the
    // request doesn't blow up. Return a minimal dormant payload so the
    // panel renders quietly and the other assertions stay focused.
    if (url.startsWith("/api/v1/inside/trust/")) {
      return json({
        product_id: BLOG.id,
        touch_time: {
          total_touch_time_hours: 0,
          decisions_resolved_count: 0,
          decisions_pending_count: 0,
          window_days: 14,
        },
        deposit_rate: { deposit_count: 0, slope_per_day: 0, window_days: 14 },
        trend_arrow: { glyph: "·", reason: "no activity in window" },
        contract_strength: { is_steady: true, amber_reason: null },
      });
    }
    throw new Error(`unexpected fetch ${url}`);
  });
}

describe("Product detail surface", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the header, recent runs, and shipped artifacts", async () => {
    // Newest-first: the running run is the latest → "working" headline; the
    // older shipped run still appears in the list and carries the artifact.
    global.fetch = installFetch({
      runs: () => [RUNNING_RUN, SHIPPED_RUN],
      deliverables: (runId) => (runId === "run-ship" ? [DELIVERABLE] : []),
    }) as unknown as typeof fetch;

    render(<ProductDetail slug="blog" />);

    // Header.
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Blog" })).toBeInTheDocument();
    });
    expect(screen.getByText(/Working on your latest direction/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /github\.com\/acme\/blog$/ })).toBeInTheDocument();

    // Recent runs — both statuses present.
    const runsSection = screen.getByRole("region", { name: "Recent runs" });
    expect(within(runsSection).getByText("Shipped")).toBeInTheDocument();
    expect(within(runsSection).getByText("Working")).toBeInTheDocument();

    // Shipped — the delivered artifact with its verdict + link.
    const shippedSection = screen.getByRole("region", { name: "Shipped" });
    expect(within(shippedSection).getByText("Add related-posts widget")).toBeInTheDocument();
    expect(within(shippedSection).getByText("opened a pull request")).toBeInTheDocument();
    expect(within(shippedSection).getByText("This is verified")).toBeInTheDocument();
    expect(within(shippedSection).getByRole("link", { name: /Open artifact/ })).toHaveAttribute(
      "href",
      "https://github.com/acme/blog/pull/15",
    );
  });

  it("links each shipped artifact to its Delivery Report (so artifacts are reachable)", async () => {
    global.fetch = installFetch({
      runs: () => [SHIPPED_RUN],
      deliverables: (runId) => (runId === "run-ship" ? [DELIVERABLE] : []),
    }) as unknown as typeof fetch;

    render(<ProductDetail slug="blog" />);

    const shippedSection = await screen.findByRole("region", { name: "Shipped" });
    // The report link points at the deliverable's report route — where the new
    // interactive artifact viewer lives.
    expect(within(shippedSection).getByRole("link", { name: /report/i })).toHaveAttribute(
      "href",
      "/deliverables/d1",
    );
  });

  it("shows the calm not-found state for an unknown slug", async () => {
    global.fetch = installFetch({
      products: () => [BLOG],
      runs: () => [],
    }) as unknown as typeof fetch;

    render(<ProductDetail slug="ghost" />);

    await waitFor(() => {
      expect(screen.getByText(/I don’t know that product/)).toBeInTheDocument();
    });
    // A way back to the Brief is offered.
    expect(screen.getByRole("link", { name: /Back to the Brief/ })).toHaveAttribute(
      "href",
      "/brief",
    );
    // Not the ready surface.
    expect(screen.queryByRole("region", { name: "Recent runs" })).not.toBeInTheDocument();
  });

  it("renders a calm inline error (not a blank page) when the core read fails", async () => {
    global.fetch = installFetch({ products: () => json("boom", 500) }) as unknown as typeof fetch;

    render(<ProductDetail slug="blog" />);

    await waitFor(() => {
      expect(screen.getByText(/Couldn’t load this product/)).toBeInTheDocument();
    });
    expect(screen.queryByRole("region", { name: "Recent runs" })).not.toBeInTheDocument();
  });

  it("shows calm empty states when the product has no runs / nothing shipped", async () => {
    global.fetch = installFetch({
      products: () => [BLOG],
      runs: () => [],
    }) as unknown as typeof fetch;

    render(<ProductDetail slug="blog" />);

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Blog" })).toBeInTheDocument();
    });
    expect(screen.getByText(/Nothing running yet/)).toBeInTheDocument();
    expect(screen.getByText(/No runs for this product yet/)).toBeInTheDocument();
    expect(screen.getByText(/Nothing shipped for this product yet/)).toBeInTheDocument();
  });

  it("shows a loading note before the read lands", async () => {
    global.fetch = installFetch({ runs: () => [SHIPPED_RUN] }) as unknown as typeof fetch;

    render(<ProductDetail slug="blog" />);

    expect(screen.getByText(/Looking at this product/)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Blog" })).toBeInTheDocument();
    });
  });

  it("always offers a way back to the Brief", async () => {
    global.fetch = installFetch({
      products: () => [BLOG],
      runs: () => [],
    }) as unknown as typeof fetch;

    render(<ProductDetail slug="blog" />);
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Blog" })).toBeInTheDocument();
    });
    expect(screen.getByRole("link", { name: "‹ Brief" })).toHaveAttribute("href", "/brief");
  });
});
