"use client";

import type { ResolvedDecision } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { relativeTime } from "./relative-time";

/**
 * One resolved row in the audit trail (Stitch "Recently resolved"). Read-only
 * history across the SAME three kinds the Pending tab judges:
 *   - "knowledge" a recorded canon decision (the directional decision kind)
 *   - "delivery"  a decided Safe-Mode delivery (approved / declined / expired)
 *   - "decision"  an answered paused-run checkpoint (the question + the answer)
 * No actions — it's history.
 */
function deliveryStatusLabel(status: string, t: ReturnType<typeof useTranslations>): string {
  if (status === "approved") return t("resolvedDeliveryApproved");
  if (status === "denied") return t("resolvedDeliveryDenied");
  return t("resolvedDeliveryExpired");
}

export default function ResolvedRow({ item }: { item: ResolvedDecision }) {
  const t = useTranslations("decisions");

  if (item.kind === "delivery") {
    return (
      <li className="decisions-row decisions-row--resolved">
        <span className="decisions-row__q">{deliveryStatusLabel(item.status, t)}</span>
        <span className="decisions-row__meta">
          <span className="decisions-chip">{t("kindDelivery")}</span>
          <span className="decisions-row__time">{relativeTime(item.resolvedAt, t)}</span>
        </span>
      </li>
    );
  }

  if (item.kind === "decision") {
    return (
      <li className="decisions-row decisions-row--resolved">
        <span className="decisions-row__q">{item.question}</span>
        {item.resolution ? (
          <span className="decisions-row__answered">
            <span className="decisions-row__answer-label">{t("resolvedAnswerLabel")}</span>
            <span className="decisions-row__answer-text">{item.resolution}</span>
          </span>
        ) : null}
        <span className="decisions-row__meta">
          <span className="decisions-chip">{t("kindDecision")}</span>
          <span className="decisions-row__time">{relativeTime(item.resolvedAt, t)}</span>
        </span>
      </li>
    );
  }

  // knowledge — a recorded canon decision (unchanged from the original row).
  return (
    <li className="decisions-row decisions-row--resolved">
      <span className="decisions-row__q">{t("outcomeRecorded")}</span>
      <span className="decisions-row__meta">
        <span className="decisions-chip decisions-chip--outcome">{item.decisionKind}</span>
        <span className="decisions-row__time">{relativeTime(item.resolvedAt, t)}</span>
      </span>
    </li>
  );
}
