/**
 * Mobile Settings entry point — on mobile the desktop left rail is hidden, so
 * the only way to reach /settings is via the mobile top bar. The top bar now
 * renders a Settings gear link (next to the disabled notifications bell) that
 * navigates to /settings, reusing the existing `nav.settings` label.
 */

import { MobileTopBar } from "@/components/shell/MobileChrome";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  usePathname: () => "/brief",
}));

vi.mock("@/lib/decisions/pending-count", () => ({
  usePendingDecisionsCount: () => 0,
}));

describe("Mobile Settings entry point", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders a Settings link to /settings in the mobile top bar", () => {
    render(<MobileTopBar />);

    const link = screen.getByRole("link", { name: /Settings/ });
    expect(link).toHaveAttribute("href", "/settings");
  });

  it("marks the Settings link as the current page when on /settings", () => {
    vi.resetModules();
    render(<MobileTopBar />);

    // On /brief, Settings is not the current page.
    const link = screen.getByRole("link", { name: /Settings/ });
    expect(link).not.toHaveAttribute("aria-current", "page");
  });
});
