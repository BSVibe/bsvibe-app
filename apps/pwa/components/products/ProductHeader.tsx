import type { ActivityTone, ProductDetailView } from "@/lib/api/types";

/** Status tone → the lone status dot colour (UX §5 — colour for status only).
 *  Reuses the Activity/Brief tone vocabulary so the surfaces feel like one. */
const TONE_CLASS: Record<ActivityTone, string> = {
  neutral: "product-head__dot--neutral",
  working: "product-head__dot--working",
  review: "product-head__dot--review",
  shipped: "product-head__dot--shipped",
  failed: "product-head__dot--failed",
};

/**
 * The product header — the focused view's headline: the product name, a single
 * plain-language status line (current state, derived from its latest run), and
 * its lone status dot. The repo link, when the product carries one, is a quiet
 * secondary affordance.
 */
export default function ProductHeader({ view }: { view: ProductDetailView }) {
  return (
    <header className="product-head">
      <div className="product-head__row">
        <span className={`product-head__dot ${TONE_CLASS[view.currentTone]}`} aria-hidden="true">
          ●
        </span>
        <h1 className="product-head__name">{view.name}</h1>
      </div>
      <p className="product-head__status">{view.currentStatus}</p>
      {view.repoUrl && (
        <a
          className="product-head__repo"
          href={view.repoUrl}
          target="_blank"
          rel="noopener noreferrer"
        >
          {view.repoUrl}
        </a>
      )}
    </header>
  );
}
