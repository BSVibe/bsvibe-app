"use client";

import CheckpointRow from "@/components/decisions/CheckpointRow";
import DeliveryRow from "@/components/decisions/DeliveryRow";
import type { PendingDecision } from "@/lib/api/types";
import { useTranslations } from "next-intl";

/**
 * "Needs you" — the hero of the unified Brief (R4). The pending decisions the
 * founder must judge are rendered INLINE, with context, using the EXISTING
 * Decisions rows so both action shapes work in place:
 *   - "delivery"  → DeliveryRow  (Approve & ship / Decline a held delivery)
 *   - "decision"  → CheckpointRow (ship-gate one-click actions OR an
 *                   ask_user_question's LLM options + an "Other" free-text)
 *
 * This is the core of the Brief/Decisions unification: a decision is an inline
 * STATE of a work-stream, resolved HERE — not on a divorced inbox tab.
 * Resolving a row calls its existing resolve endpoint and then `onResolved`,
 * which re-reads the Brief so the item leaves the list. Multiple concurrent
 * items stack as separate rows.
 *
 * Only the two inline-resolvable kinds reach here (brief.ts filters them);
 * knowledge proposals, which open a focused detail panel, stay on the Decisions
 * tab.
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
          ) : null,
        )}
      </ul>
    </section>
  );
}
