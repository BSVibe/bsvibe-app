"use client";

import { acceptProposal, rejectProposal } from "@/lib/api/decisions";
import type { Proposal } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useState } from "react";

type RowState = "idle" | "working" | "error";

/** Plain-language verb for the proposal's action (e.g. "merge-concepts" →
 *  "Merge concepts"). Mirrors Decisions.proposalVerb, inlined so the Brief
 *  doesn't pull in the whole Decisions component just for one string. */
function proposalTitle(p: Proposal): string {
  const verb = p.action_kind.replace(/-/g, " ").trim();
  return verb ? verb.charAt(0).toUpperCase() + verb.slice(1) : p.proposal_kind;
}

/** A readable label for WHAT the proposal touches — the last path segment,
 *  de-slugged (e.g. ".../merge-concepts/clamp-helper.md" → "Clamp helper"). */
function proposalTarget(path: string): string {
  const file = (path.split("/").pop() ?? path).replace(/\.md$/i, "");
  const text = file.replace(/[-_]+/g, " ").trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : path;
}

/**
 * R9 — a knowledge / canon PROPOSAL as a card in the unified Brief "Needs you".
 * Canon proposals arise WHILE doing work (a pattern worth promoting, a variant
 * worth merging), so the founder judges them inline here — the same place as the
 * other decisions — instead of a separate Decisions tab. Accept applies the
 * linked typed actions (POST /api/v1/decisions/{path}/accept); Reject resolves
 * it without applying (…/reject). Resolves inline; a failed call keeps the card
 * actionable with a calm message, and on success the container re-reads.
 */
export default function ProposalCard({
  item,
  onResolved,
}: {
  item: Proposal;
  onResolved: () => void;
}) {
  const t = useTranslations("decisions");
  const [state, setState] = useState<RowState>("idle");
  const working = state === "working";
  const title = proposalTitle(item);

  const run = async (action: "accept" | "reject") => {
    if (working) return;
    setState("working");
    try {
      if (action === "accept") {
        await acceptProposal(item.action_path);
      } else {
        await rejectProposal(item.action_path);
      }
      onResolved();
    } catch {
      setState("error");
    }
  };

  return (
    <li className="need-card need-card--proposal">
      <div className="need-card__head">
        <div className="need-card__title-wrap">
          <span className="need-card__title">{title}</span>
          <span className="need-card__product">{item.proposal_kind}</span>
        </div>
        <span className="need-card__status">
          <span className="need-card__status-dot" aria-hidden="true" />
          {t("needsYourAnswer")}
        </span>
      </div>
      <p className="need-card__body">{proposalTarget(item.action_path)}</p>
      <div className="need-card__actions">
        <button
          type="button"
          className="need-card__btn need-card__btn--primary"
          onClick={() => run("accept")}
          disabled={working}
        >
          {working ? t("working") : t("accept")}
        </button>
        <button
          type="button"
          className="need-card__btn need-card__btn--secondary"
          onClick={() => run("reject")}
          disabled={working}
        >
          {t("reject")}
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
