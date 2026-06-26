/**
 * Sidebar PRODUCTS section (components/shell/RailProducts.tsx). The left rail's
 * separate "PRODUCTS" section, below the primary nav. It:
 *
 *  - loads the workspace's products from listProducts() on mount
 *  - renders each as a link to /products/{slug} showing the name
 *  - shows a calm "No products yet" empty state when there are none
 *  - degrades to a calm note (and never crashes) when the load fails
 *  - offers a "+ Product" CTA that opens the create flow
 *  - is the product INDEX itself — the heading is plain text, NOT a link to a
 *    (removed) /products overview page
 *  - renders no trust trend glyph (removed — the rail is a plain index)
 *
 * The list loads asynchronously, so every assertion that depends on it is gated
 * behind findBy / waitFor — never a synchronous getBy right after render.
 */

import RailProducts from "@/components/shell/RailProducts";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/brief",
}));

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Routes fetch by URL substring so the products list and the fleet-trust
 *  glyph endpoint can return different bodies in the same test. */
function routedFetch(map: Record<string, unknown>) {
  return vi.fn(async (url: string) => {
    for (const [needle, body] of Object.entries(map)) {
      if (typeof url === "string" && url.includes(needle)) return jsonResponse(body);
    }
    return jsonResponse([], 404);
  });
}

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
  repo_url: "https://github.com/acme/widgets",
  created_at: "2026-05-22T00:00:00Z",
  updated_at: "2026-05-22T00:00:00Z",
};

describe("Sidebar PRODUCTS section", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders a PRODUCTS heading", async () => {
    global.fetch = vi.fn(async () => jsonResponse([])) as unknown as typeof fetch;
    render(<RailProducts />);
    expect(screen.getByRole("heading", { name: /products/i })).toBeInTheDocument();
    await screen.findByText(/No products yet/i);
  });

  it("renders each product as a link to /products/{slug}", async () => {
    global.fetch = vi.fn(async () =>
      jsonResponse([PRODUCT_A, PRODUCT_B]),
    ) as unknown as typeof fetch;

    render(<RailProducts />);

    const list = await screen.findByRole("list", { name: /products/i });
    const a = within(list).getByRole("link", { name: /Related Posts/ });
    expect(a).toHaveAttribute("href", "/products/related-posts");
    const b = within(list).getByRole("link", { name: /Widgets/ });
    expect(b).toHaveAttribute("href", "/products/widgets");
  });

  it("shows a calm empty state when there are no products", async () => {
    global.fetch = vi.fn(async () => jsonResponse([])) as unknown as typeof fetch;

    render(<RailProducts />);

    expect(await screen.findByText(/No products yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: /products/i })).not.toBeInTheDocument();
  });

  it("degrades to a calm note when the load fails (no crash)", async () => {
    global.fetch = vi.fn(async () => jsonResponse("boom", 500)) as unknown as typeof fetch;

    render(<RailProducts />);

    expect(await screen.findByText(/Couldn.t load your products/i)).toBeInTheDocument();
  });

  it("offers a + Product CTA that opens the create form", async () => {
    global.fetch = vi.fn(async () => jsonResponse([])) as unknown as typeof fetch;

    render(<RailProducts />);

    await screen.findByText(/No products yet/i);
    const newBtn = screen.getByRole("button", { name: /New product/i });
    await userEvent.click(newBtn);

    // The create form's Name field appears once the modal opens.
    await waitFor(() => {
      expect(screen.getByLabelText(/^Name$/i)).toBeInTheDocument();
    });
  });

  it("no longer shows a New-project link beside the Products heading", async () => {
    global.fetch = vi.fn(async () => jsonResponse([PRODUCT_A])) as unknown as typeof fetch;

    render(<RailProducts />);

    await screen.findByRole("list", { name: /products/i });
    // Exactly one create affordance (the bottom CTA) — the old header link is gone.
    expect(screen.getAllByRole("button", { name: /New product/i })).toHaveLength(1);
  });

  it("renders the heading as plain text, not a link to a /products overview", async () => {
    global.fetch = vi.fn(async () => jsonResponse([PRODUCT_A])) as unknown as typeof fetch;

    render(<RailProducts />);

    await screen.findByRole("list", { name: /products/i });
    const heading = screen.getByRole("heading", { name: /products/i });
    // The heading is no longer wrapped in an anchor — the rail IS the index, so
    // there is no /products overview to link to. (The overview page was removed.)
    expect(heading.closest("a")).toBeNull();
    // And no link anywhere points at the bare /products overview route.
    const overviewLink = screen
      .queryAllByRole("link")
      .find((el) => el.getAttribute("href") === "/products");
    expect(overviewLink).toBeUndefined();
  });

  it("renders no trend glyph — the rail is a plain product index", async () => {
    global.fetch = vi.fn(async () => jsonResponse([PRODUCT_A])) as unknown as typeof fetch;

    render(<RailProducts />);

    // The product list renders; the trust trend glyph was removed from the rail.
    expect(await screen.findByRole("link", { name: /Related Posts/ })).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /trust/i })).not.toBeInTheDocument();
  });
});
