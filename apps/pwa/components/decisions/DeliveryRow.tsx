"use client";

import { approveSafeModeItem, denySafeModeItem } from "@/lib/api/safemode";
import type { PendingDelivery } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useState } from "react";

type RowState = "idle" | "working" | "error";

/**
 * One Safe-Mode held-delivery row in the unified Pending list. A held outbound
 * delivery is the founder's first real "Decide" action — Approve dispatches it
 * out (POST /api/v1/safemode/{id}/approve), Decline drops it (POST …/deny with
 * a { reason } body). Resolves inline; a failed call keeps the row actionable
 * with a calm message and does NOT crash the list. On success the container
 * re-reads so the resolved item leaves Pending.
 *
 * The CARD is the tap target for its report (mobile-first): the title is the
 * one and only <a>, and `.tap-card__link::after` stretches it over the whole
 * card, so tapping any non-button part opens /deliverables/{id}. Approve /
 * Decline sit ABOVE that overlay (`.tap-card__above`) and are never nested in
 * the link, so they resolve instead of navigating.
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
  const title = item.title || t("deliveryQuestion");

  return (
    <li className="need-card need-card--delivery tap-card">
      <div className="need-card__head">
        {/* Lead with WHAT is shipping (concise title) so the approve decision
            is informed, not blind — and make that title the card-wide link to
            the report. */}
        <div className="need-card__title-wrap">
          {item.detailHref ? (
            <Link className="need-card__title tap-card__link" href={item.detailHref}>
              {title}
            </Link>
          ) : (
            <span className="need-card__title">{title}</span>
          )}
          {item.productSlug && item.productSlug !== "workspace" && (
            <span className="need-card__product">{item.productSlug}</span>
          )}
        </div>
        <span className="need-card__status">
          <span className="need-card__status-dot" aria-hidden="true" />
          {t("readyToShip")}
        </span>
      </div>
      {/* The held-delivery context becomes the card body when a title leads. */}
      {item.title && <p className="need-card__body">{t("deliveryQuestion")}</p>}
      <div className="need-card__actions tap-card__above">
        <button
          type="button"
          className="need-card__btn need-card__btn--primary"
          onClick={() => run("approve")}
          disabled={working}
        >
          {working ? t("working") : t("approve")}
        </button>
        <button
          type="button"
          className="need-card__btn need-card__btn--secondary"
          onClick={() => run("deny")}
          disabled={working}
        >
          {t("decline")}
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
