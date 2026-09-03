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

export default function WorkingNow({
  items,
  hasLiveWorker = true,
}: {
  items: ActiveWork[];
  /** When false, no worker can pick up an active run — show an honest "waiting
   *  for a worker" state instead of the ever-climbing "Working" timer.
   *  `null` = the /workers read failed: unknown, so make no claim either way. */
  hasLiveWorker?: boolean | null;
}) {
  const t = useTranslations("brief");

  return (
    <section className="working" aria-label={t("workingNow")}>
      <h2 className="section-label">{t("workingNow")}</h2>
      {items.length === 0 ? (
        <p className="working__empty">{t("allCaughtUp")}</p>
      ) : (
        <ul className="working__list">
          {items.map((w) => {
            // No live worker → the run is queued, not being worked. Present it
            // honestly (calm "waiting for a worker" pill + a one-line reason)
            // rather than a pulsing "Working" that climbs forever.
            // `=== false` on purpose: `null` is an unknown (the /workers read
            // failed), and a blip must not relabel a live run as queued.
            const waiting = hasLiveWorker === false;
            const tone = waiting ? "neutral" : STATUS_TONE[w.status];
            return (
              <li key={w.runId} className="working__card">
                <Link href={`/runs/${w.runId}`} className="working__card-link">
                  <span className={`working__pill working__pill--${tone}`}>
                    {!waiting && <span className="working__pulse" aria-hidden="true" />}
                    {waiting ? t("statusWaitingWorker") : t(STATUS_LABEL_KEY[w.status])}
                  </span>
                  <span className="working__title">{w.title ?? t("workingUntitled")}</span>
                  <span className="working__meta">
                    <span className="working__product">{w.productSlug}</span>
                    <span className="working__elapsed">
                      {waiting ? t("waitingWorkerHint") : elapsed(w.startedAt, t)}
                    </span>
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
