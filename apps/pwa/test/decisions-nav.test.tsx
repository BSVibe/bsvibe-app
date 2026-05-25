/**
 * Decisions nav wiring — the previously-inert "Decisions" item is now a real
 * route ((app)/decisions) in both the desktop left rail and the mobile tab bar,
 * and both surface a pending-count badge fed by the shared store.
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

describe("Decisions nav wiring", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
    setPendingDecisionsCount(0);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders Decisions as a real link to /decisions in the left rail", () => {
    render(<LeftRail />);

    const link = screen.getByRole("link", { name: /Decisions/ });
    expect(link).toHaveAttribute("href", "/decisions");
    // No longer a disabled placeholder button.
    expect(screen.queryByRole("button", { name: "Decisions" })).not.toBeInTheDocument();
  });

  it("renders Decisions as a real link in the mobile tab bar", () => {
    render(<MobileNav />);

    const link = screen.getByRole("link", { name: /Decisions/ });
    expect(link).toHaveAttribute("href", "/decisions");
  });

  it("shows a pending-count badge when there are pending decisions", () => {
    setPendingDecisionsCount(3);
    render(<LeftRail />);

    expect(screen.getByLabelText("3 pending")).toHaveTextContent("3");
  });

  it("hides the badge when nothing is pending", () => {
    setPendingDecisionsCount(0);
    render(<LeftRail />);

    expect(screen.queryByLabelText(/pending/)).not.toBeInTheDocument();
  });
});
