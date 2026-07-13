import type { ArtifactType, ShippedItem } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import Link from "next/link";

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
 *
 * The ROW is the tap target for its Delivery Report (mobile-first): the title is
 * the stretched link (`.tap-card__link::after` covers the whole row), replacing
 * the old separate "View report" link. The external artifact link stays above
 * the overlay (`.tap-card__above`) so it is still tappable on its own.
 */
export default function ProductShipped({ items }: { items: ShippedItem[] }) {
  const t = useTranslations("products");
  return (
    <section className="product-shipped" aria-label={t("shipped")}>
      <h2 className="section-label">{t("shipped")}</h2>
      {items.length === 0 ? (
        <p className="product-shipped__empty">{t("noShipped")}</p>
      ) : (
        <ul className="product-shipped__list">
          {items.map((item) => {
            const a = ARTIFACT[item.artifactType];
            return (
              <li key={item.id} className="product-shipped__row tap-card">
                <span
                  className={`product-shipped__icon product-shipped__icon--${a.tone}`}
                  aria-hidden="true"
                >
                  {a.glyph}
                </span>
                <div className="product-shipped__body">
                  {/* Glass-box proof: the title opens the deliverable's Delivery
                      Report (where the produced artifact CONTENT is viewable
                      inline) and its ::after stretches over the whole row. */}
                  <Link
                    className="product-shipped__title tap-card__link"
                    href={`/deliverables/${item.id}`}
                  >
                    {item.title || t("untitled")}
                  </Link>
                  <span className="product-shipped__source">{item.source}</span>
                  {item.link && (
                    <a
                      className="product-shipped__link tap-card__above"
                      href={item.link}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {t("openArtifact")}
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
