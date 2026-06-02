/**
 * TrendArrowGlyph (components/products/TrendArrowGlyph.tsx).
 *
 * The single Fleet glance glyph (design §3.2). Renders the four-state
 * ↗ → ↘ · vocabulary, with the backend `reason` shown as the native title
 * + folded into the accessible name so a screen reader announces both the
 * tone (rising / flat / falling / dormant) AND the plain-language reason.
 *
 * No numbers; no count appears in the rendered output regardless of the
 * `arrow` payload. The Fleet card is glyph + product name only — this
 * component's job is to enforce that at the type level (it takes a
 * `TrendArrow`, not a `ProductTrust`).
 */

import TrendArrowGlyph from "@/components/products/TrendArrowGlyph";
import type { TrendArrow } from "@/lib/api/trust.types";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

const ARROWS: Record<string, TrendArrow> = {
  rising: { glyph: "↗", reason: "touch ÷ deposits falling — trust rising" },
  flat: { glyph: "→", reason: "north-star ratio steady" },
  falling: { glyph: "↘", reason: "touch ÷ deposits rising — needs attention" },
  dormant: { glyph: "·", reason: "no activity in window" },
};

describe("TrendArrowGlyph", () => {
  it("renders each of the four glyphs verbatim", () => {
    const { rerender } = render(<TrendArrowGlyph arrow={ARROWS.rising} />);
    expect(screen.getByRole("img", { name: /trust rising/i })).toHaveTextContent("↗");

    rerender(<TrendArrowGlyph arrow={ARROWS.flat} />);
    expect(screen.getByRole("img", { name: /trust steady/i })).toHaveTextContent("→");

    rerender(<TrendArrowGlyph arrow={ARROWS.falling} />);
    expect(screen.getByRole("img", { name: /trust falling/i })).toHaveTextContent("↘");

    rerender(<TrendArrowGlyph arrow={ARROWS.dormant} />);
    expect(screen.getByRole("img", { name: /dormant/i })).toHaveTextContent("·");
  });

  it("surfaces the reason in the title attribute (hover tooltip)", () => {
    render(<TrendArrowGlyph arrow={ARROWS.rising} />);
    const glyph = screen.getByRole("img", { name: /trust rising/i });
    expect(glyph).toHaveAttribute("title", ARROWS.rising.reason);
  });

  it("folds the reason into the accessible name", () => {
    render(<TrendArrowGlyph arrow={ARROWS.falling} />);
    // The aria-label combines tone + reason so a screen reader reads both.
    expect(
      screen.getByRole("img", { name: /trust falling — touch . deposits rising/i }),
    ).toBeInTheDocument();
  });

  it("applies a tone-specific class so the CSS can colour each state", () => {
    const { rerender } = render(<TrendArrowGlyph arrow={ARROWS.rising} />);
    expect(screen.getByRole("img", { name: /trust rising/i })).toHaveClass("trend-arrow--rising");
    rerender(<TrendArrowGlyph arrow={ARROWS.falling} />);
    expect(screen.getByRole("img", { name: /trust falling/i })).toHaveClass("trend-arrow--falling");
  });

  it("never renders a number — only the glyph", () => {
    render(<TrendArrowGlyph arrow={ARROWS.rising} />);
    const glyph = screen.getByRole("img", { name: /trust rising/i });
    // No digits in the rendered text content (design §3.2 — Fleet card is
    // glyph + product name only, never a count).
    expect(glyph.textContent ?? "").toMatch(/^[↗→↘·]$/);
  });
});
