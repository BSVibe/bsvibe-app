"use client";

import type { AdversarialFindingItem } from "@/lib/api/types";
import { useTranslations } from "next-intl";

/** Where a finding sits: `file:line — claim`. */
function label(f: AdversarialFindingItem): string {
  const where = f.file ? `${f.file}${f.line != null ? `:${f.line}` : ""} — ` : "";
  return `${where}${f.claim}`;
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
              <li key={i}>{label(f)}</li>
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
              <li key={i}>{label(f)}</li>
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
