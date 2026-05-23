import type { ProductDetailRun } from "@/lib/api/types";

/** Calm absolute date ("May 23 · 2:14 PM"); falls back to the raw string when
 *  unparseable. No date library — keeps the bundle quiet (matches RunRow). */
function formatWhen(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const day = date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const time = date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return `${day} · ${time}`;
}

/**
 * "Recent runs" — this product's run history, newest first, each in plain
 * language with its lone status colour (the Activity/Brief vocabulary). A calm
 * read-only list: the focused per-product slice of what the AI has done. Shows
 * a quiet empty line when the product has no runs yet.
 */
export default function ProductRuns({ runs }: { runs: ProductDetailRun[] }) {
  return (
    <section className="product-runs" aria-label="Recent runs">
      <h2 className="section-label">Recent runs</h2>
      {runs.length === 0 ? (
        <p className="product-runs__empty">No runs for this product yet.</p>
      ) : (
        <ul className="product-runs__list">
          {runs.map((run) => (
            <li key={run.runId} className="product-run">
              <span className={`product-run__status product-run__status--${run.tone}`}>
                {run.statusLabel}
              </span>
              <span className="product-run__when">{formatWhen(run.updatedAt)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
