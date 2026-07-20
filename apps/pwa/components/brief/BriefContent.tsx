"use client";

import type { BriefView } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";
import NeedsYou from "./NeedsYou";
import ShippedSection from "./ShippedSection";
import WorkingNow from "./WorkingNow";

/**
 * The unified Brief home (R4) — one work-stream = one row, and a decision is an
 * inline STATE of a work-stream, not a divorced inbox tab. A single content
 * column (the app shell/rail stays), top to bottom:
 *
 *  1. Header — "Brief" title + filter chips ("All" / "Needs you N" / "Working" /
 *     "Shipped"). The chips are the home for what used to be the separate
 *     Decisions tab; they narrow the visible sections client-side.
 *  2. Needs you (hero) — the pending decisions resolved INLINE with context via
 *     the EXISTING DeliveryRow / CheckpointRow / ProposalCard (all three action
 *     shapes work in place). Resolving re-reads the Brief.
 *  3. Working — the in-flight runs (WorkingNow).
 *  4. Shipped — COLLAPSED to a count + the most-recent few + a "View all"
 *     affordance, so endless shipped history never floods the page.
 *
 * The Brief is the SINGLE home for pending decisions (all three kinds); there is
 * no separate /decisions route.
 *
 * `onNeedsYouResolved` is the container's re-read hook; it defaults to a no-op so
 * the component stays trivially testable with a ready `BriefView`.
 */
type Filter = "all" | "needs-you" | "working" | "shipped";

export default function BriefContent({
  view,
  onNeedsYouResolved = () => {},
}: {
  view: BriefView;
  onNeedsYouResolved?: () => void;
}) {
  const t = useTranslations("brief");
  const [filter, setFilter] = useState<Filter>("all");

  // Shipped section = the shipped work-stream rows (the chronological history of
  // delivered work); other terminal states are represented elsewhere (review →
  // an inline needs-you decision; the run page holds the rest).
  const shipped = useMemo(() => view.stream.filter((s) => s.status === "shipped"), [view.stream]);
  const needsCount = view.needsYou.length;

  const chips: { id: Filter; label: string; count?: number; amber?: boolean }[] = [
    { id: "all", label: t("filterAll") },
    { id: "needs-you", label: t("filterNeedsYou"), count: needsCount, amber: needsCount > 0 },
    { id: "working", label: t("filterWorking") },
    { id: "shipped", label: t("filterShipped") },
  ];

  const showNeedsYou = filter === "all" || filter === "needs-you";
  const showWorking = filter === "all" || filter === "working";
  const showShipped = filter === "all" || filter === "shipped";

  return (
    <div className="brief">
      <header className="brief__head">
        <h1 className="brief__heading">{t("heading")}</h1>
        <div className="brief__filters" role="tablist" aria-label={t("filtersLabel")}>
          {chips.map((c) => (
            <button
              key={c.id}
              type="button"
              role="tab"
              aria-selected={filter === c.id}
              className={`filter-chip${filter === c.id ? " filter-chip--on" : ""}${
                c.amber ? " filter-chip--amber" : ""
              }`}
              onClick={() => setFilter(c.id)}
            >
              {c.label}
              {c.count !== undefined && c.count > 0 && (
                <span className="filter-chip__count">{c.count}</span>
              )}
            </button>
          ))}
        </div>
      </header>

      {showNeedsYou && <NeedsYou items={view.needsYou} onResolved={onNeedsYouResolved} />}
      {showWorking && <WorkingNow items={view.working} />}
      {showShipped && <ShippedSection items={shipped} forceExpanded={filter === "shipped"} />}
    </div>
  );
}
