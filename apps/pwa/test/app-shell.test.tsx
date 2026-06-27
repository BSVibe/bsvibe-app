import AppShell from "@/components/shell/AppShell";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

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

describe("AppShell", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  it("renders the primary nav with Brief marked as the current page", () => {
    render(
      <AppShell>
        <div>page content</div>
      </AppShell>,
    );

    const briefLinks = screen.getAllByRole("link", { name: "Brief" });
    expect(briefLinks.length).toBeGreaterThan(0);
    expect(briefLinks.some((link) => link.getAttribute("aria-current") === "page")).toBe(true);
  });

  it("R18: the mobile bottom nav exposes a Products tab linking to /products", () => {
    render(
      <AppShell>
        <div>page content</div>
      </AppShell>,
    );

    // The mobile nav carries a Product entry (the desktop rail's PRODUCTS
    // section is hidden on mobile, so the rail's product index needs a home).
    const productsLink = screen
      .getAllByRole("link", { name: "Product" })
      .find((link) => link.getAttribute("href") === "/products");
    expect(productsLink).toBeDefined();
  });

  it("surfaces the account chip and the Direct affordance, and renders children", () => {
    render(
      <AppShell>
        <div>page content</div>
      </AppShell>,
    );

    expect(screen.getByText("founder@bsvibe.dev")).toBeInTheDocument();
    expect(screen.getByText("page content")).toBeInTheDocument();
    expect(screen.getAllByText("Direct").length).toBeGreaterThan(0);
  });

  it("opens the Direct overlay on ⌘K", () => {
    render(
      <AppShell>
        <div>page content</div>
      </AppShell>,
    );

    expect(screen.queryByRole("dialog", { name: "Direct" })).not.toBeInTheDocument();

    fireEvent.keyDown(window, { key: "k", metaKey: true });

    expect(screen.getByRole("dialog", { name: "Direct" })).toBeInTheDocument();
  });
});
