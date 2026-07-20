"use client";

import CheckpointRow from "@/components/decisions/CheckpointRow";
import DeliveryRow from "@/components/decisions/DeliveryRow";
import type { PendingDecision } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import ProposalCard from "./ProposalCard";

/**
 * "Needs you" — the hero of the unified Brief (R4). The pending decisions the
 * founder must judge are rendered INLINE, with context, using the EXISTING
 * Decisions rows so both action shapes work in place:
 *   - "delivery"  → DeliveryRow   (Approve & ship / Decline a held delivery)
 *   - "decision"  → CheckpointRow (ship-gate one-click actions OR an
 *                   ask_user_question's LLM options + a free-text answer)
 *   - "knowledge" → ProposalCard  (Accept / Reject a canon proposal) — R9
 *
 * This is the core of the Brief/Decisions unification: a decision is an inline
 * STATE of a work-stream, resolved HERE — not on a divorced inbox tab. ALL three
 * pending kinds (held deliveries, paused-run checkpoints, AND canon proposals
 * that arise while doing the work) are judged here; there is no separate
 * Decisions tab. Resolving a card calls its existing resolve endpoint and then `onResolved`,
 * which re-reads the Brief so the item leaves the list. Multiple concurrent
 * items stack as separate cards.
 */
export default function NeedsYou({
  items,
  onResolved,
}: {
  items: PendingDecision[];
  onResolved: () => void;
}) {
  const t = useTranslations("brief");
  if (items.length === 0) return null;

  return (
    <section className="needs-you" aria-label={t("needsYou")}>
      <h2 className="section-label section-label--amber">{t("needsYou")}</h2>
      <ul className="needs-list" aria-label={t("needsYou")}>
        {items.map((item) =>
          item.kind === "delivery" ? (
            <DeliveryRow key={item.id} item={item} onResolved={onResolved} />
          ) : item.kind === "decision" ? (
            <CheckpointRow key={item.id} item={item} onResolved={onResolved} />
          ) : item.kind === "knowledge" ? (
            <ProposalCard key={item.id} item={item.proposal} onResolved={onResolved} />
          ) : null,
        )}
      </ul>
    </section>
  );
}
