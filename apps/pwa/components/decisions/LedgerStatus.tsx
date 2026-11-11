"use client";

import type { LedgerFields } from "@/lib/api/types";
import { useTranslations } from "next-intl";

/**
 * The run's requirements-ledger verdict on a Safe Mode card: "N requirements, M confirmed",
 * and — loudly — the `unclear` / `deferred` items, so the founder sees what is NOT confirmed
 * BEFORE approving. Renders nothing for a run without a ledger; no alert when all is confirmed.
 */
export default function LedgerStatus({ ledger }: { ledger?: LedgerFields }) {
  const t = useTranslations("ledgerStatus");
  const total = ledger?.ledger_counts?.requirements_total ?? 0;
  if (total === 0) return null;
  const done = ledger?.ledger_counts?.requirements_done ?? 0;
  const attention = ledger?.ledger_attention ?? [];
  const corrected = ledger?.ledger_corrected ?? [];

  return (
    <div className={`ledger-status${attention.length > 0 ? " ledger-status--attention" : ""}`}>
      <span className="ledger-status__count">{t("confirmed", { total, done })}</span>
      {attention.length > 0 && (
        <div className="ledger-status__warning" role="alert">
          <strong>{t("attentionTitle")}</strong>
          <ul className="ledger-status__items">
            {attention.map((a) => (
              <li key={a.id}>
                {a.id}: {a.text}
              </li>
            ))}
          </ul>
        </div>
      )}
      {corrected.length > 0 && (
        <div className="ledger-status__corrected">
          <strong>{t("correctedTitle")}</strong>
          <ul className="ledger-status__items">
            {corrected.map((c) => (
              <li key={c.id}>
                {c.id}: {c.text}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
