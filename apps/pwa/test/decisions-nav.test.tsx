/**
 * Decisions nav removal (R8) — Decisions was folded into the Brief ("Needs
 * you"), so it is no longer a primary-nav tab in either the desktop left rail
 * or the mobile tab bar. The /decisions route still exists (reachable by URL),
 * but it is not advertised in the nav.
 */

import LeftRail from "@/components/shell/LeftRail";
import { MobileNav } from "@/components/shell/MobileChrome";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
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

describe("Decisions nav removal", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("does not show a Decisions tab in the left rail", () => {
    render(<LeftRail />);

    expect(screen.queryByRole("link", { name: /Decisions/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Decisions/ })).not.toBeInTheDocument();
    // The remaining primary surfaces are still present.
    expect(screen.getByRole("link", { name: /Brief/ })).toHaveAttribute("href", "/brief");
    expect(screen.getByRole("link", { name: /Knowledge/ })).toHaveAttribute("href", "/knowledge");
  });

  it("does not show a Decisions tab in the mobile tab bar", () => {
    render(<MobileNav />);

    expect(screen.queryByRole("link", { name: /Decisions/ })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Brief/ })).toHaveAttribute("href", "/brief");
  });
});
