"use client";

import NewProductForm from "@/components/shell/NewProductForm";
import { PlusIcon } from "@/components/shell/icons";
import { listProducts } from "@/lib/api/products";
import type { Product } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

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
          {products.map((p) => (
            <li key={p.id}>
              <Link href={`/products/${p.slug}`} className="products-index__item">
                <span className="products-index__dot" aria-hidden="true" />
                <span className="products-index__name">{p.name}</span>
                <span className="products-index__slug">{p.slug}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}

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
