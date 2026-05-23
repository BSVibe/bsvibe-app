import type { Concept } from "@/lib/api/types";
import { useTranslations } from "next-intl";

/**
 * "What I know" — the settled knowledge wall: canonical anchors the
 * canonicalization promoter graduated. Each row shows the concept name, a short
 * summary (empty for a freshly-promoted anchor that carries only its title),
 * and a calm connectedness signal — how many variant spellings resolve onto
 * this anchor ("N mentions"). Read-only.
 *
 * On a failed read this renders a calm inline note instead of the list, so the
 * sibling section still shows.
 */
export default function ConceptsSection({
  items,
  failed,
}: {
  items: Concept[];
  failed: boolean;
}) {
  const t = useTranslations("inside");
  return (
    <section className="inside-block" aria-label={t("whatIKnow")}>
      <header className="inside-block__head">
        <h2 className="section-label">{t("whatIKnow")}</h2>
        {!failed && items.length > 0 && <span className="inside-block__count">{items.length}</span>}
      </header>

      {failed ? (
        <p className="inside-block__note" aria-live="polite">
          {t("conceptsError")}
        </p>
      ) : items.length === 0 ? (
        <p className="inside-block__note">{t("conceptsEmpty")}</p>
      ) : (
        <ul className="inside-list">
          {items.map((concept) => (
            <li key={concept.id} className="inside-row">
              <div className="inside-row__head">
                <span className="inside-row__name">{concept.name}</span>
                {concept.alias_count > 0 && (
                  <span className="inside-row__mentions">
                    {t("mentions", { count: concept.alias_count })}
                  </span>
                )}
              </div>
              {concept.summary && <p className="inside-row__summary">{concept.summary}</p>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
