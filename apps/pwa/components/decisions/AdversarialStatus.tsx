"use client";

import type { AdversarialFindingItem } from "@/lib/api/types";
import { useTranslations } from "next-intl";

/** Where a finding sits: `file:line — claim`. */
function label(f: AdversarialFindingItem): string {
  const where = f.file ? `${f.file}${f.line != null ? `:${f.line}` : ""} — ` : "";
  return `${where}${f.claim}`;
}

/** Why a finding stands as it does: "재현이 실행되지 않음" for a suspected one whose repro never
 *  ran, the reason for any other suspected one, "변경 전 실행 불가" for a confirmed one. */
function Why({ f, confirmed }: { f: AdversarialFindingItem; confirmed: boolean }) {
  const t = useTranslations("adversarialStatus");
  const notRun = !confirmed && f.not_run === true;
  const baseline = confirmed && f.baseline_not_run === true;
  if (!notRun && !baseline && !(!confirmed && f.reason)) return null;
  return (
    <span className="adversarial-status__reason" data-testid="adversarial-finding-reason">
      {" "}
      {notRun ? (
        <strong data-testid="adversarial-not-run">{t("notRun")}</strong>
      ) : baseline ? (
        <strong data-testid="adversarial-baseline-not-run">{t("baselineNotRun")}</strong>
      ) : null}
      {f.reason && (!confirmed || baseline) ? ` ${t("reason", { reason: f.reason })}` : null}
    </span>
  );
}

/**
 * The adversarial verifier's findings, confirmed ones FIRST and the unreproduced ones apart as
 * "의심" (suspected). Report-only: never blocks anything. Renders nothing when there is nothing
 * to show; `unavailable` says so.
 */
export function AdversarialFindings({
  confirmed,
  suspected,
}: {
  confirmed: AdversarialFindingItem[];
  suspected: AdversarialFindingItem[];
}) {
  const t = useTranslations("adversarialStatus");
  return (
    <>
      {confirmed.length > 0 && (
        <div className="adversarial-status__group" data-group="confirmed">
          <span className="adversarial-status__count">
            {t("confirmed", { count: confirmed.length })}
          </span>
          <ul className="adversarial-status__items">
            {confirmed.map((f, i) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: findings have no id; the list is static.
              <li key={i}>
                {label(f)}
                <Why f={f} confirmed />
              </li>
            ))}
          </ul>
        </div>
      )}
      {suspected.length > 0 && (
        <div className="adversarial-status__group" data-group="suspected">
          <span className="adversarial-status__count">
            {t("suspected", { count: suspected.length })}
          </span>
          <ul className="adversarial-status__items">
            {suspected.map((f, i) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: findings have no id; the list is static.
              <li key={i}>
                {label(f)}
                <Why f={f} confirmed={false} />
              </li>
            ))}
          </ul>
        </div>
      )}
    </>
  );
}

export default function AdversarialStatus({
  status,
  confirmed = [],
  suspected = [],
}: {
  status?: "done" | "no_probe" | "unavailable" | "none";
  confirmed?: AdversarialFindingItem[];
  suspected?: AdversarialFindingItem[];
}) {
  const t = useTranslations("adversarialStatus");
  if (!status || status === "none") return null;
  if (status === "no_probe") {
    return (
      <div className="adversarial-status">
        <span className="adversarial-status__title">{t("title")}</span>
        <span className="adversarial-status__count">{t("noProbe")}</span>
        {(confirmed.length > 0 || suspected.length > 0) && (
          <AdversarialFindings confirmed={confirmed} suspected={suspected} />
        )}
      </div>
    );
  }
  if (status === "unavailable") {
    return (
      <div className="adversarial-status">
        <span className="adversarial-status__count">{t("unavailable")}</span>
      </div>
    );
  }
  return (
    <div className="adversarial-status">
      <span className="adversarial-status__title">{t("title")}</span>
      {confirmed.length === 0 && suspected.length === 0 ? (
        <span className="adversarial-status__count">{t("none")}</span>
      ) : (
        <AdversarialFindings confirmed={confirmed} suspected={suspected} />
      )}
    </div>
  );
}
