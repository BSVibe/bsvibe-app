"use client";

import { approveSafeModeRun } from "@/lib/api/safemode";
import type { PendingDelivery } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { relativeTime } from "./relative-time";

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
  // Newest item drives the "when" label so the group sits near the most-recent
  // delivery in the unified Pending list.
  const newest = items.reduce(
    (latest, item) => (Date.parse(item.createdAt) > Date.parse(latest.createdAt) ? item : latest),
    items[0],
  );

  return (
    <li className="decisions-row decisions-row--delivery-group">
      <span className="decisions-row__main">
        <span className="decisions-row__q">
          {t("deliveryGroupQuestion", { count: items.length })}
        </span>
        <span className="decisions-row__meta">
          <span className="decisions-chip decisions-chip--delivery">{t("kindDelivery")}</span>
          <span className="decisions-row__time">{relativeTime(newest.createdAt, t)}</span>
        </span>
      </span>
      <span className="decisions-row__actions">
        {state === "error" && (
          <span className="decisions-row__error" aria-live="polite">
            {t("resolveError")}
          </span>
        )}
        <button type="button" className="decisions-row__primary" onClick={run} disabled={working}>
          {working ? t("working") : t("approveAll", { count: items.length })}
        </button>
      </span>
    </li>
  );
}
