/**
 * Skills nav wiring — the net-new "Skills" primary surface is a real route
 * ((app)/skills) in both the desktop left rail and the mobile tab bar. It is a
 * plain read-only link (no badge — Skills carries no pending count).
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

describe("Skills nav wiring", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders Skills as a real link to /skills in the left rail", () => {
    render(<LeftRail />);

    const link = screen.getByRole("link", { name: /Skills/ });
    expect(link).toHaveAttribute("href", "/skills");
    // Not a disabled placeholder button.
    expect(screen.queryByRole("button", { name: "Skills" })).not.toBeInTheDocument();
  });

  it("renders Skills as a real link in the mobile tab bar", () => {
    render(<MobileNav />);

    const link = screen.getByRole("link", { name: /Skills/ });
    expect(link).toHaveAttribute("href", "/skills");
  });

  it("Skills carries no numeric pending-count badge", () => {
    render(<LeftRail />);

    const link = screen.getByRole("link", { name: /Skills/ });
    expect(link).not.toHaveTextContent("3");
  });
});
