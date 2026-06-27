"use client";

import { getProductDetail } from "@/lib/api/product-detail";
import type { ProductDetailView } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useState } from "react";
import BootstrapStatusPanel from "./BootstrapStatusPanel";
import ProductBindings from "./ProductBindings";
import ProductDanger from "./ProductDanger";
import ProductFiles from "./ProductFiles";
import ProductHeader from "./ProductHeader";
import ProductResources from "./ProductResources";
import ProductRuns from "./ProductRuns";
import ProductShipped from "./ProductShipped";

/** The product detail tabs (R17): work first, then the codebase, then config. */
type ProductTab = "activity" | "files" | "settings";

/**
 * The Product detail surface (`/products/[slug]`) — a focused per-product
 * window (R17 redesign): a minimal header (name + status) over three tabs —
 * 활동 (the product's runs + shipped work), 파일 (its codebase file browser),
 * 설정 (resources + connector bindings + delete). No trust/health, no repo URL,
 * and no knowledge framing — the knowledge graph + notes are GLOBAL, not
 * per-product. Replaces the old 9-panel linear stack.
 *
 * Composed entirely client-side from the list endpoints (lib/api/product-
 * detail.ts): the product is found in /api/v1/products, its runs filtered out of
 * /api/v1/runs, and each shipped run's deliverables fetched from
 * /api/v1/deliverables. States:
 *
 *  - loading    → a quiet "Looking at this product…" note
 *  - not-found  → a calm "I don't know that product" + a way back to the Brief
 *                 (an unknown slug resolves to `null`, NOT an error)
 *  - error      → a calm inline note (never a blank page or an error wall)
 *  - ready      → the header + recent runs + shipped artifacts
 *
 * Read-only by design — no mutations on this surface.
 */
type Loaded =
  | { state: "loading" }
  | { state: "error" }
  | { state: "not-found" }
  | { state: "ready"; view: ProductDetailView };

export default function ProductDetail({ slug }: { slug: string }) {
  const [loaded, setLoaded] = useState<Loaded>({ state: "loading" });
  const [tab, setTab] = useState<ProductTab>("activity");
  const t = useTranslations("products");

  useEffect(() => {
    let active = true;
    setLoaded({ state: "loading" });
    getProductDetail(slug)
      .then((view) => {
        if (!active) return;
        setLoaded(view ? { state: "ready", view } : { state: "not-found" });
      })
      .catch(() => {
        if (active) setLoaded({ state: "error" });
      });
    return () => {
      active = false;
    };
  }, [slug]);

  return (
    <div className="product">
      <Link className="product__back" href="/brief">
        {t("back")}
      </Link>

      {loaded.state === "loading" && (
        <p className="product__loading-note" aria-busy="true">
          {t("loadingNote")}
        </p>
      )}

      {loaded.state === "not-found" && (
        <section className="product-empty" aria-label={t("region")}>
          <p className="product-empty__line">{t("notFoundLine")}</p>
          <p className="product-empty__sub">
            {t("notFoundSubPrefix")}
            <Link href="/brief">{t("backToBrief")}</Link>
            {t("notFoundSubSuffix")}
          </p>
        </section>
      )}

      {loaded.state === "error" && (
        <section className="product-empty" aria-label={t("region")}>
          <p className="product-empty__line">{t("errorLine")}</p>
          <p className="product-empty__sub">{t("errorSub")}</p>
        </section>
      )}

      {loaded.state === "ready" && (
        <>
          <ProductHeader view={loaded.view} />
          {/* Calm one-line status while the repo bootstrap runs in the
              background. Renders null when there's nothing to show, so a static
              product gets zero chrome (the only repo-adjacent surface, kept
              minimal per R17). */}
          <BootstrapStatusPanel productId={loaded.view.id} />

          {/* Tabs (R17): the work first (활동), then the codebase (파일), then
              config (설정). Replaces the old 9-panel linear stack; no trust /
              knowledge framing (the knowledge graph + notes are global). */}
          <div className="product-tabs" role="tablist" aria-label={t("tabsLabel")}>
            {(
              [
                ["activity", t("tabActivity")],
                ["files", t("tabFiles")],
                ["settings", t("tabSettings")],
              ] as const
            ).map(([id, label]) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={tab === id}
                className={`product-tab${tab === id ? " product-tab--on" : ""}`}
                onClick={() => setTab(id)}
              >
                {label}
              </button>
            ))}
          </div>

          {tab === "activity" && (
            <>
              <ProductRuns runs={loaded.view.runs} />
              <ProductShipped items={loaded.view.shipped} />
            </>
          )}
          {tab === "files" && <ProductFiles productId={loaded.view.id} />}
          {tab === "settings" && (
            <>
              <ProductResources productId={loaded.view.id} />
              <ProductBindings productId={loaded.view.id} />
              <ProductDanger productId={loaded.view.id} productName={loaded.view.name} />
            </>
          )}
        </>
      )}
    </div>
  );
}
