import type { ArtifactType, ShippedItem } from "@/lib/api/types";

/** Per-artifact-type marker (UX §4 — deliverables are polymorphic), matched to
 *  the Brief/Activity glyph vocabulary so the surfaces feel like one product. */
const ARTIFACT: Record<ArtifactType, { glyph: string; tone: string }> = {
  pr: { glyph: "◆", tone: "pr" },
  doc: { glyph: "▤", tone: "doc" },
  image: { glyph: "▦", tone: "image" },
  slides: { glyph: "▥", tone: "slides" },
  file: { glyph: "▢", tone: "file" },
  email: { glyph: "✉", tone: "email" },
};

/**
 * "Shipped" — the focused view of what this product has delivered: each shipped
 * artifact with its summary, type marker, the "This is verified" proof verdict,
 * and an external link when the artifact has an addressable landing spot. Shows
 * a calm empty line when the product hasn't shipped anything yet.
 */
export default function ProductShipped({ items }: { items: ShippedItem[] }) {
  return (
    <section className="product-shipped" aria-label="Shipped">
      <h2 className="section-label">Shipped</h2>
      {items.length === 0 ? (
        <p className="product-shipped__empty">Nothing shipped for this product yet.</p>
      ) : (
        <ul className="product-shipped__list">
          {items.map((item) => {
            const a = ARTIFACT[item.artifactType];
            return (
              <li key={item.id} className="product-shipped__row">
                <span
                  className={`product-shipped__icon product-shipped__icon--${a.tone}`}
                  aria-hidden="true"
                >
                  {a.glyph}
                </span>
                <div className="product-shipped__body">
                  <span className="product-shipped__title">{item.title}</span>
                  <span className="product-shipped__source">{item.source}</span>
                  {item.link && (
                    <a
                      className="product-shipped__link"
                      href={item.link}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      Open artifact
                    </a>
                  )}
                </div>
                <span className="product-shipped__verdict">{item.verdict}</span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
