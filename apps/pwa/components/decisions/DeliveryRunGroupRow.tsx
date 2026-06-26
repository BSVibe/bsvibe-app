"use client";

import { approveSafeModeRun } from "@/lib/api/safemode";
import type { PendingDelivery } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useState } from "react";

type RowState = "idle" | "working" | "error";

/**
 * B12a — multi-artifact delivery group for one Run.
 *
 * Safe Mode is the per-Run transactional container (Workflow §1.2): when the
 * agent loop emits N partial Deliver events during one run, the founder sees a
 * single grouped row instead of N separate Approve / Decline rows. The
 * "Approve all (N)" action hits POST /api/v1/safemode/runs/{runId}/approve and
 * dispatches every queued item for that run together. A failed call keeps the
 * row actionable with a calm message; on success the container re-reads so
 * the resolved items leave Pending.
 *
 * Per-item Approve / Decline remains available via the individual DeliveryRow
 * — this group is rendered ABOVE the run's individual rows when ≥2 items share
 * the same runId. A single-item run keeps the existing per-row UX (no group
 * header).
 */
export default function DeliveryRunGroupRow({
  runId,
  items,
  onResolved,
}: {
  runId: string;
  items: PendingDelivery[];
  onResolved: () => void;
}) {
  const t = useTranslations("decisions");
  const [state, setState] = useState<RowState>("idle");

  async function run() {
    if (state === "working") return;
    setState("working");
    try {
      await approveSafeModeRun(runId);
      onResolved();
    } catch {
      setState("error");
    }
  }

  const working = state === "working";

  return (
    <li className="need-card need-card--delivery">
      <div className="need-card__head">
        <div className="need-card__title-wrap">
          <span className="need-card__title">
            {t("deliveryGroupQuestion", { count: items.length })}
          </span>
        </div>
        <span className="need-card__status">
          <span className="need-card__status-dot" aria-hidden="true" />
          {t("readyToShip")}
        </span>
      </div>
      <div className="need-card__actions">
        <button
          type="button"
          className="need-card__btn need-card__btn--primary"
          onClick={run}
          disabled={working}
        >
          {working ? t("working") : t("approveAll", { count: items.length })}
        </button>
        {state === "error" && (
          <span className="need-card__error" aria-live="polite">
            {t("resolveError")}
          </span>
        )}
      </div>
    </li>
  );
}
