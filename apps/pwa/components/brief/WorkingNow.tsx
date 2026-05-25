import type { ActiveWork } from "@/lib/api/types";
import { STATUS_LABEL_KEY, STATUS_TONE } from "@/lib/runs/status";
import { useTranslations } from "next-intl";
import Link from "next/link";

/**
 * "Working on now" — the hero of the merged Work-Home surface. The founder's
 * top question is "what is BSVibe doing right now?", so the active runs are the
 * dominant visual element: each as a card with a live status pill, the work
 * title (the run's Direction), the product, and how long it's been running.
 *
 * A calm "all caught up" line when nothing is in flight.
 */

/** Elapsed-since-start in a calm phrase ("4m in" / "2h in"), i18n-driven. */
function elapsed(iso: string, t: ReturnType<typeof useTranslations>): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const minutes = Math.max(0, Math.floor((Date.now() - then) / 60000));
  const hours = Math.floor(minutes / 60);
  if (minutes < 1) return t("elapsedJustNow");
  if (hours < 1) return t("elapsedMinutes", { n: minutes });
  return t("elapsedHours", { n: hours });
}

export default function WorkingNow({ items }: { items: ActiveWork[] }) {
  const t = useTranslations("brief");

  return (
    <section className="working" aria-label={t("workingNow")}>
      <h2 className="section-label">{t("workingNow")}</h2>
      {items.length === 0 ? (
        <p className="working__empty">{t("allCaughtUp")}</p>
      ) : (
        <ul className="working__list">
          {items.map((w) => {
            const tone = STATUS_TONE[w.status];
            return (
              <li key={w.runId} className="working__card">
                <Link href={`/runs/${w.runId}`} className="working__card-link">
                  <span className={`working__pill working__pill--${tone}`}>
                    <span className="working__pulse" aria-hidden="true" />
                    {t(STATUS_LABEL_KEY[w.status])}
                  </span>
                  <span className="working__title">{w.title ?? t("workingUntitled")}</span>
                  <span className="working__meta">
                    <span className="working__product">{w.productSlug}</span>
                    <span className="working__elapsed">{elapsed(w.startedAt, t)}</span>
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
