/**
 * ProductDanger — the per-product delete control. Two-step (Delete → Confirm),
 * then DELETE /api/v1/products/{id} and route back to the list. fetch + router
 * mocked.
 */

import ProductDanger from "@/components/products/ProductDanger";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const push = vi.fn();
const refresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, refresh }),
}));

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

describe("ProductDanger", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
    push.mockClear();
    refresh.mockClear();
  });
  afterEach(() => vi.restoreAllMocks());

  it("requires a second confirm before deleting (no accidental single click)", async () => {
    const fetchMock = vi.fn();
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<ProductDanger productId="p-1" productName="bsvibe-app" />);
    fireEvent.click(screen.getByRole("button", { name: "Delete product" }));

    // The confirm step names the product; nothing has been deleted yet.
    expect(screen.getByText(/Delete “bsvibe-app”/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("DELETEs the product on confirm and routes back to the list", async () => {
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<ProductDanger productId="p-1" productName="bsvibe-app" />);
    fireEvent.click(screen.getByRole("button", { name: "Delete product" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete permanently" }));

    await waitFor(() => expect(push).toHaveBeenCalledWith("/products"));
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/api/v1/products/p-1");
    expect(init.method).toBe("DELETE");
  });

  it("shows a calm error and stays put when the delete fails", async () => {
    global.fetch = vi.fn(
      async () => new Response("boom", { status: 500 }),
    ) as unknown as typeof fetch;

    render(<ProductDanger productId="p-1" productName="bsvibe-app" />);
    fireEvent.click(screen.getByRole("button", { name: "Delete product" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete permanently" }));

    await waitFor(() =>
      expect(screen.getByText(/Couldn’t delete that product/)).toBeInTheDocument(),
    );
    expect(push).not.toHaveBeenCalled();
  });
});
