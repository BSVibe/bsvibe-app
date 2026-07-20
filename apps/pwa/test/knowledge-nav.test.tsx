/**
 * Knowledge nav wiring — the "Knowledge" item (formerly "Inside") is a real
 * route ((app)/knowledge) in both the desktop left rail and the mobile tab bar.
 * It is a plain read-only link (no badge — Knowledge carries no pending count).
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

describe("Knowledge nav wiring", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders Knowledge as a real link to /knowledge in the left rail", () => {
    render(<LeftRail />);

    const link = screen.getByRole("link", { name: /Knowledge/ });
    expect(link).toHaveAttribute("href", "/knowledge");
    // No longer a disabled placeholder button, and no stale "Inside" label.
    expect(screen.queryByRole("button", { name: "Knowledge" })).not.toBeInTheDocument();
    expect(screen.queryByText("Inside")).not.toBeInTheDocument();
  });

  it("renders Knowledge as a real link in the mobile tab bar", () => {
    render(<MobileNav />);

    const link = screen.getByRole("link", { name: /Knowledge/ });
    expect(link).toHaveAttribute("href", "/knowledge");
  });

  it("Knowledge carries no numeric pending-count badge", () => {
    render(<LeftRail />);

    const link = screen.getByRole("link", { name: /Knowledge/ });
    expect(link).not.toHaveTextContent("3");
  });
});
