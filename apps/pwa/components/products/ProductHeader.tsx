import type { ActivityTone, ProductDetailView } from "@/lib/api/types";
import { useTranslations } from "next-intl";

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
 * The product header — the focused view's headline: the product name, its lone
 * status dot, and a single plain-language status line (derived from its latest
 * run). Deliberately minimal — no repo URL (a product's repo is an internal
 * detail the founder may not have or may differ on; R17) and no trust/health.
 */
export default function ProductHeader({ view }: { view: ProductDetailView }) {
  const t = useTranslations("products");
  return (
    <header className="product-head">
      <div className="product-head__row">
        <span className={`product-head__dot ${TONE_CLASS[view.currentTone]}`} aria-hidden="true">
          ●
        </span>
        <h1 className="product-head__name">{view.name}</h1>
      </div>
      <p className="product-head__status">{t(view.currentStatusKey)}</p>
    </header>
  );
}
