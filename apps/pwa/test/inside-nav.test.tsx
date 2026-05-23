/**
 * Inside nav wiring — the previously-inert "Inside" item is now a real route
 * ((app)/inside) in both the desktop left rail and the mobile tab bar. It is a
 * plain read-only link (no badge — Inside carries no pending count).
 */

import LeftRail from "@/components/shell/LeftRail";
import { MobileNav } from "@/components/shell/MobileChrome";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { setPendingDecisionsCount } from "@/lib/decisions/pending-count";
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  usePathname: () => "/brief",
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
}));

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

describe("Inside nav wiring", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
    setPendingDecisionsCount(0);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders Inside as a real link to /inside in the left rail", () => {
    render(<LeftRail onDirect={() => {}} />);

    const link = screen.getByRole("link", { name: /Inside/ });
    expect(link).toHaveAttribute("href", "/inside");
    // No longer a disabled placeholder button.
    expect(screen.queryByRole("button", { name: "Inside" })).not.toBeInTheDocument();
  });

  it("renders Inside as a real link in the mobile tab bar", () => {
    render(<MobileNav />);

    const link = screen.getByRole("link", { name: /Inside/ });
    expect(link).toHaveAttribute("href", "/inside");
  });

  it("Inside carries no pending-count badge even when decisions are pending", () => {
    setPendingDecisionsCount(3);
    render(<LeftRail onDirect={() => {}} />);

    const link = screen.getByRole("link", { name: /Inside/ });
    expect(link).not.toHaveTextContent("3");
  });
});
