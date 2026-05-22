import type { LaneState, ProductLane } from "@/lib/api/types";

/** Lane-state glyph + accessible label (UX §3.3). Color only for the two
 *  states that carry status meaning: amber needs-you, green shipped. */
const GLYPH: Record<LaneState, { mark: string; label: string }> = {
  working: { mark: "●", label: "working" },
  "needs-you": { mark: "●", label: "needs you" },
  triggered: { mark: "↑", label: "just triggered" },
  shipped: { mark: "✓", label: "shipped" },
  idle: { mark: "○", label: "idle" },
};

/**
 * "Your products" — calm status lanes, not metric cards (UX §3.2 principle 2).
 * Each lane is one product: glyph + name + a state tag + a plain-language
 * status line. Machinery (rounds, cost) stays invisible.
 */
export default function ProductLanes({ lanes }: { lanes: ProductLane[] }) {
  return (
    <section className="lanes" aria-label="Your products">
      <h2 className="section-label">Your products</h2>
      <ul className="lanes__list">
        {lanes.map((lane) => {
          const g = GLYPH[lane.state];
          return (
            <li key={lane.id} className={`lane lane--${lane.state}`}>
              <span className="lane__glyph" aria-hidden="true">
                {g.mark}
              </span>
              <div className="lane__body">
                <div className="lane__head">
                  <span className="lane__name">{lane.name}</span>
                  <span className="lane__state">{g.label}</span>
                </div>
                {lane.state !== "idle" && <p className="lane__status">{lane.status}</p>}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
