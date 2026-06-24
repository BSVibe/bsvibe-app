"use client";

import { relativeTime } from "@/components/decisions/relative-time";
import type { RunStatus, WorkStreamItem } from "@/lib/api/types";
import { STATUS_LABEL_KEY, STATUS_TONE } from "@/lib/runs/status";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useMemo, useState } from "react";

type Filter = "all" | "shipped" | "needs-you" | "failed";

const FILTERS: { id: Filter; labelKey: string; match: (s: RunStatus) => boolean }[] = [
  { id: "all", labelKey: "filterAll", match: () => true },
  { id: "shipped", labelKey: "filterShipped", match: (s) => s === "shipped" },
  { id: "needs-you", labelKey: "filterNeedsYou", match: (s) => s === "review_ready" },
  { id: "failed", labelKey: "filterFailed", match: (s) => s === "failed" },
];

/**
 * "Work stream" — the merged Brief/Activity history: every done run, newest
 * first, with its STATUS as the lead signal (a coloured marker the founder reads
 * instantly), the work title, product, relative time, and a "View report" link
 * when the run produced a deliverable. Quiet filter pills narrow by outcome.
 */
export default function WorkStream({ items }: { items: WorkStreamItem[] }) {
  const t = useTranslations("brief");
  const [filter, setFilter] = useState<Filter>("all");

  const active = FILTERS.find((f) => f.id === filter) ?? FILTERS[0];
  const visible = useMemo(() => items.filter((i) => active.match(i.status)), [items, active]);

  return (
    <section className="stream" aria-label={t("workStream")}>
      <div className="stream__head">
        <h2 className="section-label">{t("workStream")}</h2>
        <div className="stream__filters" role="tablist" aria-label={t("workStream")}>
          {FILTERS.map((f) => (
            <button
              key={f.id}
              type="button"
              role="tab"
              aria-selected={filter === f.id}
              className={`stream__filter${filter === f.id ? " stream__filter--on" : ""}`}
              onClick={() => setFilter(f.id)}
            >
              {t(f.labelKey)}
            </button>
          ))}
        </div>
      </div>

      {visible.length === 0 ? (
        <p className="stream__empty">{t("streamEmpty")}</p>
      ) : (
        <ul className="stream__list">
          {visible.map((item) => {
            const tone = STATUS_TONE[item.status];
            const statusLabel = t(STATUS_LABEL_KEY[item.status]);
            // The whole row title is the link (consistent with the Decisions /
            // Needs-you rows, which also link the title) — a deliverable opens
            // its report, otherwise the run.
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
                    {/* Status as text, not colour alone — the marker is just
                        reinforcement. */}
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
