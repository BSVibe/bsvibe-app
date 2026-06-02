"use client";

import NewProductForm from "@/components/shell/NewProductForm";
import { PlusIcon } from "@/components/shell/icons";
import { listProducts } from "@/lib/api/products";
import { getFleetTrust } from "@/lib/api/trust";
import type { TrendArrow } from "@/lib/api/trust.types";
import type { Product } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import GlyphLegendTooltip from "./GlyphLegendTooltip";
import TrendArrowGlyph from "./TrendArrowGlyph";

/**
 * Full-page products overview. A calm row list (not a uniform icon-card grid —
 * each row carries the product's own name + slug + optional repo link, the
 * Notion-craft register the rest of the app uses) with a prominent "+ New
 * product" CTA that opens the shared create form in a native <dialog>.
 *
 * Mirrors RailProducts' load / failure / empty / create lifecycle so the two
 * surfaces behave identically; this is the full-page sibling the compact rail
 * list and the Brief mobile embed both link to.
 */
type ListState = { data: Product[]; failed: boolean } | null;

export default function ProductsIndex() {
  const [list, setList] = useState<ListState>(null);
  // Fleet trust glyphs (Lift M4b): keyed by product_id. A failed read OR a
  // missing entry both fall back to "no glyph" so a backend regression
  // can't blank the products list (design §3.4: trust is calm, not load-
  // bearing for product discovery).
  const [trust, setTrust] = useState<Map<string, TrendArrow>>(new Map());
  const [creating, setCreating] = useState(false);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const t = useTranslations("productsIndex");

  async function load() {
    try {
      setList({ data: await listProducts(), failed: false });
    } catch {
      setList({ data: [], failed: true });
    }
  }

  useEffect(() => {
    let active = true;
    listProducts()
      .then((data) => active && setList({ data, failed: false }))
      .catch(() => active && setList({ data: [], failed: true }));
    return () => {
      active = false;
    };
  }, []);

  // Fetch the Fleet trust glyphs once on mount. Design §3.4 — Fleet is a
  // glance not a monitor, so no SSE; the page reloads on navigation.
  useEffect(() => {
    let active = true;
    getFleetTrust()
      .then((res) => {
        if (!active) return;
        const next = new Map<string, TrendArrow>();
        for (const entry of res.products) next.set(entry.product_id, entry.trend_arrow);
        setTrust(next);
      })
      .catch(() => {
        /* Trust is non-load-bearing; calm no-op leaves cards glyph-less. */
      });
    return () => {
      active = false;
    };
  }, []);

  // Drive the native <dialog> from `creating` — showModal() gives the backdrop,
  // focus trap, and Escape-to-close. try/catch keeps jsdom (no showModal) safe.
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    try {
      if (creating && !dialog.open) dialog.showModal();
      if (!creating && dialog.open) dialog.close();
    } catch {
      dialog.open = creating;
    }
  }, [creating]);

  const products = list && !list.failed ? list.data : [];

  return (
    <div className="products-index">
      <header className="products-index__head">
        <h1 className="products-index__heading">{t("heading")}</h1>
        <button
          type="button"
          className="products-index__cta"
          onClick={() => setCreating(true)}
          title={t("newProduct")}
        >
          <PlusIcon />
          <span>{t("newProduct")}</span>
        </button>
      </header>
      <p className="products-index__lede">{t("lede")}</p>

      {list === null ? (
        <p className="products-index__note" aria-busy="true">
          {t("loading")}
        </p>
      ) : list.failed ? (
        <p className="products-index__note" aria-live="polite">
          {t("loadError")}
        </p>
      ) : products.length === 0 ? (
        <p className="products-index__empty">{t("empty")}</p>
      ) : (
        <ul className="products-index__list" aria-label={t("listLabel")}>
          {products.map((p) => {
            const arrow = trust.get(p.id);
            return (
              <li key={p.id}>
                <Link href={`/products/${p.slug}`} className="products-index__item">
                  <span className="products-index__dot" aria-hidden="true" />
                  <span className="products-index__name">{p.name}</span>
                  {arrow ? (
                    <TrendArrowGlyph arrow={arrow} className="products-index__arrow" />
                  ) : null}
                  <span className="products-index__slug">{p.slug}</span>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
      <GlyphLegendTooltip hasGlyphs={trust.size > 0} />

      <dialog
        ref={dialogRef}
        className="new-product-modal"
        aria-label={t("newProduct")}
        onClose={() => setCreating(false)}
      >
        {creating ? (
          <div className="new-product-modal__panel">
            <p className="new-product-modal__title">{t("newProduct")}</p>
            <NewProductForm
              onCreated={() => {
                setCreating(false);
                load();
              }}
              onCancel={() => setCreating(false)}
            />
          </div>
        ) : null}
      </dialog>
    </div>
  );
}
