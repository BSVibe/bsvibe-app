"use client";

import { getProductDetail } from "@/lib/api/product-detail";
import type { ProductDetailView } from "@/lib/api/types";
import Link from "next/link";
import { useEffect, useState } from "react";
import ProductHeader from "./ProductHeader";
import ProductRuns from "./ProductRuns";
import ProductShipped from "./ProductShipped";

/**
 * The Product detail surface (`/products/[slug]`) — a focused per-product
 * window: the product's name + current status, its recent runs (with
 * plain-language statuses), and the artifacts it has shipped.
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
        ‹ Brief
      </Link>

      {loaded.state === "loading" && (
        <p className="product__loading-note" aria-busy="true">
          Looking at this product…
        </p>
      )}

      {loaded.state === "not-found" && (
        <section className="product-empty" aria-label="Product">
          <p className="product-empty__line">I don&rsquo;t know that product.</p>
          <p className="product-empty__sub">
            It may have been renamed or removed. <Link href="/brief">Back to the Brief</Link>.
          </p>
        </section>
      )}

      {loaded.state === "error" && (
        <section className="product-empty" aria-label="Product">
          <p className="product-empty__line">Couldn&rsquo;t load this product just now.</p>
          <p className="product-empty__sub">Try again in a moment.</p>
        </section>
      )}

      {loaded.state === "ready" && (
        <>
          <ProductHeader view={loaded.view} />
          <ProductRuns runs={loaded.view.runs} />
          <ProductShipped items={loaded.view.shipped} />
        </>
      )}
    </div>
  );
}
