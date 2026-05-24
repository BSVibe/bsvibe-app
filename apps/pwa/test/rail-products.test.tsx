/**
 * Sidebar PRODUCTS section (components/shell/RailProducts.tsx). The left rail's
 * separate "PRODUCTS" section, below the primary nav. It:
 *
 *  - loads the workspace's products from listProducts() on mount
 *  - renders each as a link to /products/{slug} showing the name
 *  - shows a calm "No projects yet" empty state when there are none
 *  - degrades to a calm note (and never crashes) when the load fails
 *  - offers a "+ New project" affordance that opens the create flow
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
    expect(screen.getByText(/products/i)).toBeInTheDocument();
    await screen.findByText(/No projects yet/i);
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

    expect(await screen.findByText(/No projects yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: /products/i })).not.toBeInTheDocument();
  });

  it("degrades to a calm note when the load fails (no crash)", async () => {
    global.fetch = vi.fn(async () => jsonResponse("boom", 500)) as unknown as typeof fetch;

    render(<RailProducts />);

    expect(await screen.findByText(/Couldn.t load your projects/i)).toBeInTheDocument();
  });

  it("offers a + New project affordance that opens the create form", async () => {
    global.fetch = vi.fn(async () => jsonResponse([])) as unknown as typeof fetch;

    render(<RailProducts />);

    await screen.findByText(/No projects yet/i);
    const newBtn = screen.getByRole("button", { name: /New project/i });
    await userEvent.click(newBtn);

    // The create form's Name field appears once the modal opens.
    await waitFor(() => {
      expect(screen.getByLabelText(/^Name$/i)).toBeInTheDocument();
    });
  });
});
