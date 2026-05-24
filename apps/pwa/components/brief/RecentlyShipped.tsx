import type { ArtifactType, ShippedItem } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import Link from "next/link";

/** Per-artifact-type marker (UX §4 — deliverables are polymorphic). */
const ARTIFACT: Record<ArtifactType, { glyph: string; tone: string }> = {
  pr: { glyph: "◆", tone: "pr" },
  doc: { glyph: "▤", tone: "doc" },
  image: { glyph: "▦", tone: "image" },
  slides: { glyph: "▥", tone: "slides" },
  file: { glyph: "▢", tone: "file" },
  email: { glyph: "✉", tone: "email" },
};

/**
 * "Recently shipped" — quiet reassurance (UX §3.2 principle 3): each shipped
 * deliverable with its proof verdict, mixed artifact types.
 */
export default function RecentlyShipped({ items }: { items: ShippedItem[] }) {
  const t = useTranslations("brief");
  if (items.length === 0) return null;

  return (
    <section className="shipped" aria-label={t("recentlyShipped")}>
      <h2 className="section-label">{t("recentlyShipped")}</h2>
      <ul className="shipped__list">
        {items.map((item) => {
          const a = ARTIFACT[item.artifactType];
          return (
            <li key={item.id} className="shipped__row">
              <span className={`shipped__icon shipped__icon--${a.tone}`} aria-hidden="true">
                {a.glyph}
              </span>
              <div className="shipped__body">
                <span className="shipped__title">{item.title}</span>
                <span className="shipped__meta">
                  {item.productSlug} · {item.source}
                </span>
                {/* Glass-box proof: open the deliverable's Delivery Report. */}
                <Link className="shipped__report-link" href={`/deliverables/${item.id}`}>
                  {t("viewReport")}
                </Link>
              </div>
              <span className="shipped__verdict">{item.verdict}</span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
