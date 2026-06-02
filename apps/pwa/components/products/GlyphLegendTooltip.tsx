"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

const SESSION_KEY = "bsvibe:trend-arrow-legend-seen";

/** Has the legend popover already been shown this session? Tucked into
 *  sessionStorage so it survives client-side navigations but resets on a tab
 *  close — the design only asks for "first time per session." Safely degrades
 *  to "already seen" in jsdom / private modes where sessionStorage throws. */
function hasSeenLegend(): boolean {
  if (typeof window === "undefined") return true;
  try {
    return window.sessionStorage.getItem(SESSION_KEY) === "1";
  } catch {
    return true;
  }
}

function markLegendSeen(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(SESSION_KEY, "1");
  } catch {
    /* private mode / disabled storage — silent no-op is correct here. */
  }
}

/** First-visit-per-session legend popover for the Fleet trend-arrow glyphs.
 *
 *  Renders nothing if the founder has already seen it (sessionStorage), if
 *  there are no glyphs to explain (`hasGlyphs=false`), or after the founder
 *  dismisses it. Lists all four states ↗ → ↘ · with their meanings — design
 *  §3.2 legend table. Calm dismiss button; no auto-dismiss timer so the
 *  founder can actually read it. */
export default function GlyphLegendTooltip({ hasGlyphs }: { hasGlyphs: boolean }) {
  const t = useTranslations("trust.legend");
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    if (!hasGlyphs) return;
    if (hasSeenLegend()) return;
    setVisible(true);
  }, [hasGlyphs]);

  if (!visible) return null;

  function dismiss() {
    markLegendSeen();
    setVisible(false);
  }

  return (
    <aside className="trend-arrow-legend" role="note" aria-label={t("title")}>
      <p className="trend-arrow-legend__title">{t("title")}</p>
      <ul className="trend-arrow-legend__list">
        <li className="trend-arrow-legend__row">
          <span className="trend-arrow trend-arrow--rising" aria-hidden="true">
            ↗
          </span>
          <span>{t("rising")}</span>
        </li>
        <li className="trend-arrow-legend__row">
          <span className="trend-arrow trend-arrow--flat" aria-hidden="true">
            →
          </span>
          <span>{t("flat")}</span>
        </li>
        <li className="trend-arrow-legend__row">
          <span className="trend-arrow trend-arrow--falling" aria-hidden="true">
            ↘
          </span>
          <span>{t("falling")}</span>
        </li>
        <li className="trend-arrow-legend__row">
          <span className="trend-arrow trend-arrow--dormant" aria-hidden="true">
            ·
          </span>
          <span>{t("dormant")}</span>
        </li>
      </ul>
      <button type="button" className="trend-arrow-legend__dismiss" onClick={dismiss}>
        {t("dismiss")}
      </button>
    </aside>
  );
}
