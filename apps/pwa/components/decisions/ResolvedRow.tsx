"use client";

import type { DecisionLogEntry } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { relativeTime } from "./relative-time";

/**
 * One resolved-decision row in the audit trail (Stitch "Recently resolved").
 * Read-only: shows the recorded outcome (the directional decision kind, e.g.
 * `must-link`) + when it was decided. No actions — it's history.
 */
export default function ResolvedRow({ item }: { item: DecisionLogEntry }) {
  const t = useTranslations("decisions");

  return (
    <li className="decisions-row decisions-row--resolved">
      <span className="decisions-row__q">{t("outcomeRecorded")}</span>
      <span className="decisions-row__meta">
        <span className="decisions-chip decisions-chip--outcome">{item.decision_kind}</span>
        <span className="decisions-row__time">{relativeTime(item.created_at, t)}</span>
      </span>
    </li>
  );
}
