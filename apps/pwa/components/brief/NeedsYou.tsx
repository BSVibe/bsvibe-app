import type { NeedsYouItem } from "@/lib/api/types";

/**
 * The "Needs you" strip — the one thing that genuinely requires the founder
 * (UX §3.2 principle 1), pinned to the top. Empty → a calm quiet state.
 */
export default function NeedsYou({ items }: { items: NeedsYouItem[] }) {
  if (items.length === 0) {
    return (
      <section className="needs-you needs-you--empty" aria-label="Needs you">
        <p className="needs-you__clear">Nothing needs you right now.</p>
      </section>
    );
  }

  return (
    <section className="needs-you" aria-label="Needs you">
      <header className="needs-you__head">
        <span className="needs-you__title">
          Needs you <span aria-hidden="true">👋</span>
        </span>
        <span className="needs-you__count">{items.length}</span>
      </header>
      <ul className="needs-you__list">
        {items.map((item) => (
          <li key={item.id} className="needs-you__row">
            <span className="needs-you__product">{item.productSlug}</span>
            <span className="needs-you__sep" aria-hidden="true">
              —
            </span>
            <span className="needs-you__q">{item.question}</span>
            <span className="needs-you__chev" aria-hidden="true">
              ›
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
