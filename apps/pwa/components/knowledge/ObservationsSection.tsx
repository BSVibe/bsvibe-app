import type { Observation } from "@/lib/api/types";
import { useTranslations } from "next-intl";

/**
 * "Recently observed" — the raw, unpromoted garden notes the SettleWorker
 * deposits per verified work step (the learning half of the trust ratchet).
 * Each row shows the note title, a short excerpt, its tags, and when it was
 * captured. Read-only.
 *
 * On a failed read this renders a calm inline note instead of the list, so the
 * sibling section still shows.
 */
export default function ObservationsSection({
  items,
  failed,
}: {
  items: Observation[];
  failed: boolean;
}) {
  const t = useTranslations("knowledge");
  return (
    <section className="inside-block" aria-label={t("recentlyObserved")}>
      <header className="inside-block__head">
        <h2 className="section-label">{t("recentlyObserved")}</h2>
        {!failed && items.length > 0 && <span className="inside-block__count">{items.length}</span>}
      </header>

      {failed ? (
        <p className="inside-block__note" aria-live="polite">
          {t("observationsError")}
        </p>
      ) : items.length === 0 ? (
        <p className="inside-block__note">{t("observationsEmpty")}</p>
      ) : (
        <ul className="inside-list">
          {items.map((obs) => (
            <li key={obs.id} className="inside-row">
              <div className="inside-row__head">
                <span className="inside-row__name">{obs.title}</span>
                {obs.captured_at && (
                  <span className="inside-row__when">{formatWhen(obs.captured_at)}</span>
                )}
              </div>
              {obs.excerpt && <p className="inside-row__summary">{obs.excerpt}</p>}
              {obs.tags.length > 0 && (
                <ul className="inside-tags">
                  {obs.tags.map((tag) => (
                    <li key={tag} className="inside-tag">
                      {tag}
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** Calm absolute date ("May 23"); falls back to the raw string if unparseable.
 *  Keeps the surface legible without pulling in a date library. */
function formatWhen(captured: string): string {
  const date = new Date(captured);
  if (Number.isNaN(date.getTime())) return captured;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
