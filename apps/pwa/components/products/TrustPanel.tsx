"use client";

import { getProductTrust } from "@/lib/api/trust";
import type { ProductTrustResponse } from "@/lib/api/trust.types";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

type PanelState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; data: ProductTrustResponse };

/** Format the touch-time hours as a calm human-readable phrase.
 *
 *  Design §4.3 prescribes plain text — "12 hours of human review" reads
 *  more naturally than "12.3h". We round to one decimal for non-integers
 *  and strip the decimal when it's exactly N.0. */
function formatHours(hours: number, t: (k: string) => string): string {
  if (hours <= 0) return t("touchHoursNone");
  const rounded = Math.round(hours * 10) / 10;
  const display = Number.isInteger(rounded) ? `${rounded}` : rounded.toFixed(1);
  return display;
}

/** Format the slope as "+1.2/day" or "−0.4/day" or "steady" within ε. */
function formatSlope(slope: number, t: (k: string) => string): string {
  if (Math.abs(slope) < 0.05) return t("slopeSteady");
  const sign = slope > 0 ? "+" : "−";
  return `${sign}${Math.abs(slope).toFixed(1)}/day`;
}

/** Map the trend-arrow glyph to its trend-line wording. */
function trendLineKey(glyph: ProductTrustResponse["trend_arrow"]["glyph"]): string {
  switch (glyph) {
    case "↗":
      return "trendRising";
    case "↘":
      return "trendFalling";
    case "·":
      return "trendDormant";
    default:
      return "trendFlat";
  }
}

/** L3 Inside trust strip (design §4.3) — the calm four-line summary.
 *
 *  Renders four plain-text lines: trend, touch time, deposits, and contract
 *  strength. The contract-strength line carries the only colour treatment
 *  (amber when the goodhart cross-check triggers — design §2.1 Signal A+B);
 *  everything else stays in the body-text register. No charts, no tables,
 *  no progress bars — §4.3 prescribed the calm shape, and any sparkline is
 *  deferred polish per Q6.
 *
 *  Read-only by design; on a fetch failure renders a calm "Couldn't read
 *  the trust signals" line rather than blowing away the rest of the page.
 *  Null fields (new product, no events) render as "—" gracefully. */
export default function TrustPanel({ productId }: { productId: string }) {
  const t = useTranslations("trust.panel");
  const [state, setState] = useState<PanelState>({ status: "loading" });

  useEffect(() => {
    let active = true;
    setState({ status: "loading" });
    getProductTrust(productId)
      .then((data) => {
        if (active) setState({ status: "ready", data });
      })
      .catch(() => {
        if (active) setState({ status: "error" });
      });
    return () => {
      active = false;
    };
  }, [productId]);

  if (state.status === "loading") {
    return (
      <section className="trust-panel trust-panel--loading" aria-busy="true">
        <p className="trust-panel__loading">{t("loading")}</p>
      </section>
    );
  }

  if (state.status === "error") {
    return (
      <section className="trust-panel trust-panel--error">
        <p className="trust-panel__error">{t("error")}</p>
      </section>
    );
  }

  const { data } = state;
  const trendKey = trendLineKey(data.trend_arrow.glyph);
  const hoursDisplay = formatHours(data.touch_time.total_touch_time_hours, t);
  const depositCount = data.deposit_rate.deposit_count;
  const slopeDisplay = formatSlope(data.deposit_rate.slope_per_day, t);
  const contractSteady = data.contract_strength.is_steady;
  const amberReason = data.contract_strength.amber_reason ?? "";

  return (
    <section className="trust-panel" aria-label={t("regionLabel")}>
      <h2 className="trust-panel__heading">{t("heading")}</h2>
      <ul className="trust-panel__list">
        <li className="trust-panel__line trust-panel__line--trend">
          <span className="trust-panel__label">{t("trendLabel")}</span>
          <span className="trust-panel__value">{t(trendKey)}</span>
        </li>
        <li className="trust-panel__line trust-panel__line--touch">
          <span className="trust-panel__label">{t("touchLabel")}</span>
          <span className="trust-panel__value">
            {t("touchValue", { hours: hoursDisplay, days: data.touch_time.window_days })}
          </span>
        </li>
        <li className="trust-panel__line trust-panel__line--deposit">
          <span className="trust-panel__label">{t("depositLabel")}</span>
          <span className="trust-panel__value">
            {t("depositValue", { count: depositCount, slope: slopeDisplay })}
          </span>
        </li>
        <li
          className={`trust-panel__line trust-panel__line--contract${
            contractSteady ? "" : " trust-panel__line--amber"
          }`}
        >
          <span className="trust-panel__label">{t("contractLabel")}</span>
          <span className="trust-panel__value">
            {contractSteady
              ? t("contractSteady")
              : t("contractAmber", { reason: amberReason || t("contractAmberFallback") })}
          </span>
        </li>
      </ul>
    </section>
  );
}
