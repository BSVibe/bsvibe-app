"use client";

import type { TrendArrow, TrendGlyph } from "@/lib/api/trust.types";
import { useTranslations } from "next-intl";

/** Maps each glyph to a calm BEM modifier so the CSS can colour it independently
 *  (without re-rendering the literal arrow character). The modifier vocabulary
 *  matches the four-state design §3.2 set. */
const TONE_BY_GLYPH: Record<TrendGlyph, string> = {
  "↗": "rising",
  "→": "flat",
  "↘": "falling",
  "·": "dormant",
};

/** A single Fleet glance trend-arrow glyph — design §3.2.
 *
 *  Renders the glyph + a native title (the backend-provided reason) so a hover
 *  reveals it without a JS popover. The legend popover lives separately
 *  (`GlyphLegendTooltip`) and only appears the first time per session.
 *
 *  Per design §3.2 the Fleet card shows the glyph + product name ONLY — no
 *  numbers ever (those live in the hover tooltip / Inside trust panel). This
 *  component therefore takes only the `TrendArrow` shape, not the full
 *  `ProductTrust` (so you can't accidentally render a count beside it).
 */
export default function TrendArrowGlyph({
  arrow,
  className,
}: {
  arrow: TrendArrow;
  className?: string;
}) {
  const t = useTranslations("trust");
  const tone = TONE_BY_GLYPH[arrow.glyph];
  const label = t(`glyphLabel.${tone}`);
  return (
    <span
      className={`trend-arrow trend-arrow--${tone}${className ? ` ${className}` : ""}`}
      role="img"
      aria-label={`${label} — ${arrow.reason}`}
      title={arrow.reason}
      data-glyph={arrow.glyph}
    >
      {arrow.glyph}
    </span>
  );
}
