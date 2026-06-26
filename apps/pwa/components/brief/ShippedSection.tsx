"use client";

import { relativeTime } from "@/components/decisions/relative-time";
import type { WorkStreamItem } from "@/lib/api/types";
import { STATUS_LABEL_KEY, STATUS_TONE } from "@/lib/runs/status";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useState } from "react";

/** How many shipped rows show before the founder asks for the full history.
 *  Shipped accumulates endlessly, so the section stays quiet by default. */
const COLLAPSED_COUNT = 4;
/** A week, for the "N this week" sub-count. */
const WEEK_MS = 7 * 24 * 3600_000;

/**
 * "Shipped" (R4) — the quiet tail of the unified Brief. Shipped work accumulates
 * forever, so the section is COLLAPSED by default: a calm header with the total
 * count (+ "N this week" when derivable), then only the most-recent few rows,
 * and a "View all" affordance that expands the full list. Each row keeps its
 * existing link to its report (where rollback lives).
 *
 * `forceExpanded` is set when the Shipped filter chip is active, so the chip and
 * the local View-all toggle both reveal the full list.
 */
export default function ShippedSection({
  items,
  forceExpanded = false,
}: {
  items: WorkStreamItem[];
  forceExpanded?: boolean;
}) {
  const t = useTranslations("brief");
  const [expanded, setExpanded] = useState(false);
  const showAll = expanded || forceExpanded;

  const total = items.length;
  const since = Date.now() - WEEK_MS;
  const thisWeek = items.filter((i) => Date.parse(i.updatedAt) >= since).length;
  const visible = showAll ? items : items.slice(0, COLLAPSED_COUNT);

  return (
    <section className="shipped" aria-label={t("shipped")}>
      <div className="shipped__head">
        <h2 className="section-label">{t("shipped")}</h2>
        <span className="shipped__count">
          {thisWeek > 0
            ? t("shippedCountWithWeek", { total, week: thisWeek })
            : t("shippedCount", { total })}
        </span>
        {!forceExpanded && total > COLLAPSED_COUNT && (
          <button
            type="button"
            className="shipped__view-all"
            aria-expanded={expanded}
            onClick={() => setExpanded((v) => !v)}
          >
            {expanded ? t("shippedCollapse") : t("shippedViewAll")}
          </button>
        )}
      </div>

      {total === 0 ? (
        <p className="stream__empty">{t("shippedEmpty")}</p>
      ) : (
        <ul className="stream__list">
          {visible.map((item) => {
            const tone = STATUS_TONE[item.status];
            const statusLabel = t(STATUS_LABEL_KEY[item.status]);
            const href = item.deliverableId
              ? `/deliverables/${item.deliverableId}`
              : `/runs/${item.runId}`;
            return (
              <li key={item.runId} className="stream__row">
                <span className={`stream__marker stream__marker--${tone}`} aria-hidden="true" />
                <div className="stream__body">
                  <Link className="stream__title stream__title--link" href={href}>
                    {item.title ?? statusLabel}
                  </Link>
                  <span className="stream__meta">
                    <span className={`stream__status stream__status--${tone}`}>{statusLabel}</span>
                    <span className="stream__product">{item.productSlug}</span>
                    <span className="stream__time">{relativeTime(item.updatedAt, t)}</span>
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
