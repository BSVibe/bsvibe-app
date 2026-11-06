"use client";

import type { ReviewFields } from "@/lib/api/types";
import { useTranslations } from "next-intl";

/**
 * The independent review of the run's last passed round on a Safe Mode card (report-only —
 * it never blocks Approve): "높음 N · 중간 N · 낮음 N" plus the top findings as
 * `file:line — claim`. `unavailable` says so; `none` (no review ran) renders nothing.
 */
export default function ReviewStatus({ review }: { review?: ReviewFields }) {
  const t = useTranslations("reviewStatus");
  const status = review?.review_status ?? "none";
  if (status === "none") return null;
  if (status === "unavailable") {
    return (
      <div className="review-status">
        <span className="review-status__count">{t("unavailable")}</span>
      </div>
    );
  }
  const counts = review?.review_counts ?? {};
  const findings = review?.review_findings ?? [];

  return (
    <div className="review-status">
      <span className="review-status__count">
        {t("counts", {
          high: counts.high ?? 0,
          medium: counts.medium ?? 0,
          low: counts.low ?? 0,
        })}
      </span>
      {findings.length > 0 && (
        <ul className="review-status__items">
          {findings.map((f, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: findings have no id; the list is static.
            <li key={i}>
              {f.file ? `${f.file}${f.line != null ? `:${f.line}` : ""} — ` : ""}
              {f.claim}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
