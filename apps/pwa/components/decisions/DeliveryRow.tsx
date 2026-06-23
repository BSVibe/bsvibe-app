"use client";

import { approveSafeModeItem, denySafeModeItem } from "@/lib/api/safemode";
import type { PendingDelivery } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useState } from "react";
import { relativeTime } from "./relative-time";

type RowState = "idle" | "working" | "error";

/**
 * One Safe-Mode held-delivery row in the unified Pending list. A held outbound
 * delivery is the founder's first real "Decide" action — Approve dispatches it
 * out (POST /api/v1/safemode/{id}/approve), Decline drops it (POST …/deny with
 * a { reason } body). Resolves inline; a failed call keeps the row actionable
 * with a calm message and does NOT crash the list. On success the container
 * re-reads so the resolved item leaves Pending.
 */
export default function DeliveryRow({
  item,
  onResolved,
}: {
  item: PendingDelivery;
  onResolved: () => void;
}) {
  const t = useTranslations("decisions");
  const [state, setState] = useState<RowState>("idle");

  async function run(action: "approve" | "deny") {
    if (state === "working") return;
    setState("working");
    try {
      if (action === "approve") {
        await approveSafeModeItem(item.itemId);
      } else {
        await denySafeModeItem(item.itemId);
      }
      onResolved();
    } catch {
      setState("error");
    }
  }

  const working = state === "working";

  return (
    <li className="decisions-row decisions-row--delivery">
      <span className="decisions-row__main">
        {/* Lead with WHAT is shipping (concise title) so the approve decision
            is informed, not blind; the generic question becomes the subtitle. */}
        <span className="decisions-row__q">{item.title || t("deliveryQuestion")}</span>
        {item.title && <span className="decisions-row__sub">{t("deliveryQuestion")}</span>}
        <span className="decisions-row__meta">
          <span className="decisions-chip decisions-chip--delivery">{t("kindDelivery")}</span>
          {item.productSlug && item.productSlug !== "workspace" && (
            <span className="decisions-row__product">{item.productSlug}</span>
          )}
          {item.detailHref && (
            <Link className="decisions-row__view" href={item.detailHref}>
              {t("viewProof")}
            </Link>
          )}
          <span className="decisions-row__time">{relativeTime(item.createdAt, t)}</span>
        </span>
      </span>
      <span className="decisions-row__actions">
        {state === "error" && (
          <span className="decisions-row__error" aria-live="polite">
            {t("resolveError")}
          </span>
        )}
        <button
          type="button"
          className="decisions-row__secondary"
          onClick={() => run("deny")}
          disabled={working}
        >
          {t("decline")}
        </button>
        <button
          type="button"
          className="decisions-row__primary"
          onClick={() => run("approve")}
          disabled={working}
        >
          {working ? t("working") : t("approve")}
        </button>
      </span>
    </li>
  );
}
