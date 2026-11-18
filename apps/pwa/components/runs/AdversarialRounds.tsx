"use client";

import { AdversarialFindings } from "@/components/decisions/AdversarialStatus";
import type { AdversarialFindingItem, RunVerificationRound } from "@/lib/api/types";
import { useTranslations } from "next-intl";

const SEVERITY_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 };

/** Split a stored blob's findings into confirmed / suspected, most severe first. */
export function splitFindings(round: RunVerificationRound): {
  confirmed: AdversarialFindingItem[];
  suspected: AdversarialFindingItem[];
} {
  const all = [...(round.adversarial?.findings ?? [])].sort(
    (a, b) => (SEVERITY_RANK[a.severity] ?? 3) - (SEVERITY_RANK[b.severity] ?? 3),
  );
  return {
    confirmed: all.filter((f) => f.confirmed === true),
    suspected: all.filter((f) => f.confirmed !== true),
  };
}

/** The run detail's round list: each round that ran the adversarial verifier, confirmed first
 *  and unreproduced findings as "의심". Rounds without it are left out. */
export default function AdversarialRounds({ rounds }: { rounds?: RunVerificationRound[] }) {
  const t = useTranslations("adversarialStatus");
  const shown = (rounds ?? []).filter((r) => r.adversarial);
  if (shown.length === 0) return null;
  return (
    <ul className="run-verification__adversarial-rounds">
      {shown.map((r) => {
        const { confirmed, suspected } = splitFindings(r);
        return (
          <li key={r.round} data-round={r.round}>
            <span className="adversarial-status__title">
              {t("round", { round: r.round })} · {t("title")}
            </span>
            {r.adversarial?.status === "unavailable" ? (
              <span className="adversarial-status__count">
                {t("unavailable")}
                {r.adversarial.reason ? (
                  <span className="adversarial-status__reason" data-testid="adversarial-reason">
                    {" "}
                    {t("reason", { reason: r.adversarial.reason })}
                  </span>
                ) : null}
              </span>
            ) : confirmed.length === 0 && suspected.length === 0 ? (
              <span className="adversarial-status__count">{t("none")}</span>
            ) : (
              <AdversarialFindings confirmed={confirmed} suspected={suspected} />
            )}
          </li>
        );
      })}
    </ul>
  );
}
