import type { Concept } from "@/lib/api/types";
import { useTranslations } from "next-intl";

/**
 * "What I know" — the settled knowledge wall: canonical anchors the
 * canonicalization promoter graduated. Each row shows the concept name, a short
 * summary (empty for a freshly-promoted anchor that carries only its title),
 * and a calm connectedness signal — how many variant spellings resolve onto
 * this anchor ("N mentions").
 *
 * Each row is a button that opens the read-only inspector via `onSelect`. The
 * `filter` (from the surface's search box) narrows the list by concept name +
 * alias; when it filters everything out, a calm "no matches" note shows instead
 * of an empty list. On a failed read this renders a calm inline note instead of
 * the list, so the sibling section still shows.
 */
function matchesFilter(concept: Concept, needle: string): boolean {
  if (!needle) return true;
  const haystack = [concept.name, ...concept.aliases].join(" ").toLowerCase();
  return haystack.includes(needle.toLowerCase());
}

export default function ConceptsSection({
  items,
  failed,
  filter = "",
  onSelect,
}: {
  items: Concept[];
  failed: boolean;
  filter?: string;
  onSelect?: (id: string) => void;
}) {
  const t = useTranslations("knowledge");
  const visible = items.filter((c) => matchesFilter(c, filter));
  return (
    <section className="inside-block" aria-label={t("whatIKnow")}>
      <header className="inside-block__head">
        <h2 className="section-label">{t("whatIKnow")}</h2>
        {!failed && visible.length > 0 && (
          <span className="inside-block__count">{visible.length}</span>
        )}
      </header>

      {failed ? (
        <p className="inside-block__note" aria-live="polite">
          {t("conceptsError")}
        </p>
      ) : items.length === 0 ? (
        <p className="inside-block__note">{t("conceptsEmpty")}</p>
      ) : visible.length === 0 ? (
        <p className="inside-block__note">{t("conceptsNoMatch")}</p>
      ) : (
        <ul className="inside-list">
          {visible.map((concept) => (
            <li key={concept.id} className="inside-row inside-row--clickable">
              <button
                type="button"
                className="inside-row__button"
                onClick={() => onSelect?.(concept.id)}
              >
                <span className="inside-row__head">
                  <span className="inside-row__name">{concept.name}</span>
                  {concept.alias_count > 0 && (
                    <span className="inside-row__mentions">
                      {t("mentions", { count: concept.alias_count })}
                    </span>
                  )}
                </span>
                {concept.summary && <span className="inside-row__summary">{concept.summary}</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
