import type { LaneState, ProductLane } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import Link from "next/link";

/** Lane-state glyph (UX §3.3). The accessible/visible state label comes from
 *  the `brief.laneState` catalog. Color only for the two states that carry
 *  status meaning: amber needs-you, green shipped. */
const GLYPH: Record<LaneState, string> = {
  working: "●",
  "needs-you": "●",
  triggered: "↑",
  shipped: "✓",
  idle: "○",
};

/**
 * "Your products" — calm status lanes, not metric cards (UX §3.2 principle 2).
 * Each lane is one product: glyph + name + a state tag + a plain-language
 * status line. Machinery (rounds, cost) stays invisible.
 *
 * Each lane is a link into that product's focused detail view
 * (`/products/<slug>`) — clicking a lane opens its recent runs + shipped
 * artifacts. The lane rendering is unchanged; it is just wrapped in a link.
 */
export default function ProductLanes({ lanes }: { lanes: ProductLane[] }) {
  const t = useTranslations("brief");
  return (
    <section className="lanes" aria-label={t("yourProducts")}>
      <h2 className="section-label">{t("yourProducts")}</h2>
      <ul className="lanes__list">
        {lanes.map((lane) => {
          return (
            <li key={lane.id} className={`lane lane--${lane.state}`}>
              <Link className="lane__link" href={`/products/${lane.slug}`}>
                <span className="lane__glyph" aria-hidden="true">
                  {GLYPH[lane.state]}
                </span>
                <div className="lane__body">
                  <div className="lane__head">
                    <span className="lane__name">{lane.name}</span>
                    <span className="lane__state">{t(`laneState.${lane.state}`)}</span>
                  </div>
                  {lane.state !== "idle" && <p className="lane__status">{lane.status}</p>}
                </div>
              </Link>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
