import type { Concept } from "@/lib/api/types";

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
  return (
    <section className="inside-block" aria-label="What I know">
      <header className="inside-block__head">
        <h2 className="section-label">What I know</h2>
        {!failed && items.length > 0 && <span className="inside-block__count">{items.length}</span>}
      </header>

      {failed ? (
        <p className="inside-block__note" aria-live="polite">
          Couldn&rsquo;t load what I know just now — try again in a moment.
        </p>
      ) : items.length === 0 ? (
        <p className="inside-block__note">No settled concepts yet.</p>
      ) : (
        <ul className="inside-list">
          {items.map((concept) => (
            <li key={concept.id} className="inside-row">
              <div className="inside-row__head">
                <span className="inside-row__name">{concept.name}</span>
                {concept.alias_count > 0 && (
                  <span className="inside-row__mentions">
                    {concept.alias_count} {concept.alias_count === 1 ? "mention" : "mentions"}
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
