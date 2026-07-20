/**
 * Nav after the Decisions fold (C3) — Decisions was folded into the Brief
 * ("Needs you") and the /decisions route removed entirely, so the primary nav
 * is EXACTLY Brief / Knowledge / Skills in both the desktop left rail and the
 * mobile tab bar. There is no Decisions tab anywhere.
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

  it("shows EXACTLY Brief / Knowledge / Skills in the left rail (no Decisions)", () => {
    render(<LeftRail />);

    expect(screen.queryByRole("link", { name: /Decisions/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Decisions/ })).not.toBeInTheDocument();
    // The three canonical primary surfaces are present.
    expect(screen.getByRole("link", { name: /Brief/ })).toHaveAttribute("href", "/brief");
    expect(screen.getByRole("link", { name: /Knowledge/ })).toHaveAttribute("href", "/knowledge");
    expect(screen.getByRole("link", { name: /Skills/ })).toHaveAttribute("href", "/skills");
  });

  it("does not show a Decisions tab in the mobile tab bar", () => {
    render(<MobileNav />);

    expect(screen.queryByRole("link", { name: /Decisions/ })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Brief/ })).toHaveAttribute("href", "/brief");
  });
});
