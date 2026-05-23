/**
 * Activity nav wiring — the new "Activity" item is a real route ((app)/activity)
 * in both the desktop left rail and the mobile tab bar. It is a plain read-only
 * link (no badge — Activity carries no pending count).
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

describe("Activity nav wiring", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
    setPendingDecisionsCount(0);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders Activity as a real link to /activity in the left rail", () => {
    render(<LeftRail onDirect={() => {}} />);

    const link = screen.getByRole("link", { name: /Activity/ });
    expect(link).toHaveAttribute("href", "/activity");
    expect(screen.queryByRole("button", { name: "Activity" })).not.toBeInTheDocument();
  });

  it("renders Activity as a real link in the mobile tab bar", () => {
    render(<MobileNav />);

    const link = screen.getByRole("link", { name: /Activity/ });
    expect(link).toHaveAttribute("href", "/activity");
  });

  it("Activity carries no pending-count badge even when decisions are pending", () => {
    setPendingDecisionsCount(3);
    render(<LeftRail onDirect={() => {}} />);

    const link = screen.getByRole("link", { name: /Activity/ });
    expect(link).not.toHaveTextContent("3");
  });
});
