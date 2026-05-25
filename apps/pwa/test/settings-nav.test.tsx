/**
 * Settings nav wiring — the previously-inert "Settings" foot item is now a real
 * route ((app)/settings) in the desktop left rail. It is a plain link (no
 * pending badge — Settings carries no count) and no longer a disabled
 * "Coming soon" placeholder.
 */

import LeftRail from "@/components/shell/LeftRail";
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

describe("Settings nav wiring", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
    setPendingDecisionsCount(0);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders Settings as a real link to /settings in the left rail", () => {
    render(<LeftRail />);

    const link = screen.getByRole("link", { name: /Settings/ });
    expect(link).toHaveAttribute("href", "/settings");
    // No longer a disabled placeholder button.
    expect(screen.queryByRole("button", { name: "Settings" })).not.toBeInTheDocument();
  });

  it("Settings carries no pending-count badge", () => {
    setPendingDecisionsCount(3);
    render(<LeftRail />);

    const link = screen.getByRole("link", { name: /Settings/ });
    expect(link).not.toHaveTextContent("3");
  });
});
