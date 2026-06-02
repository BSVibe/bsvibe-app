/**
 * GlyphLegendTooltip (components/products/GlyphLegendTooltip.tsx).
 *
 * The first-visit-per-session legend popover. Lists all four ↗ → ↘ ·
 * meanings and is dismissable. Session-storage keeps it from re-appearing
 * after the first dismiss. Renders nothing when no glyphs are present
 * (no point legending a glyph-less page).
 */

import GlyphLegendTooltip from "@/components/products/GlyphLegendTooltip";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

describe("GlyphLegendTooltip", () => {
  beforeEach(() => {
    try {
      window.sessionStorage.clear();
    } catch {
      /* jsdom always provides sessionStorage; defensive clear. */
    }
  });
  afterEach(() => {
    try {
      window.sessionStorage.clear();
    } catch {
      /* see above */
    }
  });

  it("renders the four-state legend on first visit", () => {
    render(<GlyphLegendTooltip hasGlyphs={true} />);
    expect(screen.getByRole("note", { name: /trust arrows/i })).toBeInTheDocument();
    expect(screen.getByText(/Touch time dropping/i)).toBeInTheDocument();
    expect(screen.getByText(/Touch time stable/i)).toBeInTheDocument();
    expect(screen.getByText(/Touch time rising/i)).toBeInTheDocument();
    expect(screen.getByText(/Dormant or not enough data/i)).toBeInTheDocument();
  });

  it("renders nothing when there are no glyphs to explain", () => {
    render(<GlyphLegendTooltip hasGlyphs={false} />);
    expect(screen.queryByRole("note", { name: /trust arrows/i })).not.toBeInTheDocument();
  });

  it("hides after the founder dismisses + remembers via sessionStorage", async () => {
    render(<GlyphLegendTooltip hasGlyphs={true} />);
    const dismiss = screen.getByRole("button", { name: /got it/i });
    await userEvent.click(dismiss);
    expect(screen.queryByRole("note", { name: /trust arrows/i })).not.toBeInTheDocument();
    expect(window.sessionStorage.getItem("bsvibe:trend-arrow-legend-seen")).toBe("1");
  });

  it("does not re-appear when sessionStorage already records a prior visit", () => {
    window.sessionStorage.setItem("bsvibe:trend-arrow-legend-seen", "1");
    render(<GlyphLegendTooltip hasGlyphs={true} />);
    expect(screen.queryByRole("note", { name: /trust arrows/i })).not.toBeInTheDocument();
  });
});
