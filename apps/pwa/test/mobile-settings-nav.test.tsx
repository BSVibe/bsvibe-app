/**
 * Mobile top bar — on mobile the desktop left rail is hidden, so the only way to
 * reach /settings is via the mobile top bar's Settings link (next to the
 * disabled notifications bell). The top-bar title resolves by the FIRST path
 * segment, so nested routes (e.g. `/settings/general`, the redirect target of
 * `/settings`) still read their section name rather than the "BSVibe" wordmark.
 */

import { MobileTopBar } from "@/components/shell/MobileChrome";
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let pathname = "/brief";

vi.mock("next/navigation", () => ({
  usePathname: () => pathname,
}));

describe("Mobile top bar", () => {
  beforeEach(() => {
    pathname = "/brief";
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders a Settings link to /settings in the mobile top bar", () => {
    render(<MobileTopBar />);

    const link = screen.getByRole("link", { name: /Settings/ });
    expect(link).toHaveAttribute("href", "/settings");
  });

  it("does not mark Settings as the current page when on /brief", () => {
    render(<MobileTopBar />);

    const link = screen.getByRole("link", { name: /Settings/ });
    expect(link).not.toHaveAttribute("aria-current", "page");
  });

  it("shows the section title for the current page", () => {
    pathname = "/brief";
    render(<MobileTopBar />);
    expect(screen.getByText("Brief")).toBeInTheDocument();
  });

  it("shows 'Settings' on a NESTED settings route (the /settings redirect target)", () => {
    pathname = "/settings/general";
    render(<MobileTopBar />);
    // The title resolves by first segment → "Settings", NOT the BSVibe wordmark.
    expect(screen.getByText("Settings")).toBeInTheDocument();
    expect(screen.queryByText("BSVibe")).not.toBeInTheDocument();
  });

  it.each([
    ["/deliverables/abc-123", "Delivery"],
    ["/runs/def-456", "Run"],
    ["/products/e2e-hello", "Product"],
  ])("shows the surface label on detail route %s (not the BSVibe wordmark)", (path, label) => {
    pathname = path;
    render(<MobileTopBar />);
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.queryByText("BSVibe")).not.toBeInTheDocument();
  });
});
