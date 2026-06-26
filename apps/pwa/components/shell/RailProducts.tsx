"use client";

import { listProducts } from "@/lib/api/products";
import type { Product } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import NewProductForm from "./NewProductForm";
import { PlusIcon } from "./icons";

/**
 * The left rail's "PRODUCTS" section (Stitch design's PRODUCTS heading): a
 * separate section BELOW the primary nav listing the workspace's products, each
 * a link to its `/products/{slug}` detail. This rail IS the product index —
 * there is no separate `/products` overview page. A calm "No products yet"
 * empty state and a prominent "+ Product" CTA (where the old "+ Direct" rail
 * button sat — Direct is now the omnipresent FAB) opens the create flow in a
 * modal, so product creation lives entirely in the rail.
 *
 * The list loads on mount and re-reads after a successful create so the section
 * reflects the server. A failed read degrades to a calm inline note rather than
 * a blanked rail (it must never crash the shell). Mirrors how the other surfaces
 * (Connectors / ExecutorWorkers) load + recover.
 */
type ListState = { data: Product[]; failed: boolean } | null;

export default function RailProducts() {
  const [list, setList] = useState<ListState>(null);
  const [creating, setCreating] = useState(false);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const pathname = usePathname();
  const t = useTranslations("shell.products");

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

  // Drive the native <dialog> from `creating`: showModal() gives the backdrop,
  // focus trap, and Escape-to-close for free. The try/catch keeps this safe in
  // jsdom (where showModal() throws "Not implemented") — falling back to the
  // plain `open` attribute so the form stays in the accessibility tree and the
  // create flow remains exercisable under test.
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
    <section className="rail-products" aria-label={t("sectionLabel")}>
      <header className="rail-products__head">
        {/* The rail IS the product index — there's no separate overview page,
            so the heading is plain text (the per-product rows below link to
            each product's /products/{slug} detail). */}
        <h2 className="rail-products__heading">{t("heading")}</h2>
      </header>

      {list === null ? (
        <p className="rail-products__note" aria-busy="true">
          {t("loading")}
        </p>
      ) : list.failed ? (
        <p className="rail-products__note" aria-live="polite">
          {t("loadError")}
        </p>
      ) : products.length === 0 ? (
        <p className="rail-products__empty">{t("empty")}</p>
      ) : (
        <ul className="rail-products__list" aria-label={t("listLabel")}>
          {products.map((p) => {
            const href = `/products/${p.slug}`;
            const active = pathname === href;
            return (
              <li key={p.id}>
                <Link
                  href={href}
                  className="rail-products__item"
                  aria-current={active ? "page" : undefined}
                >
                  <span className="rail-products__dot" aria-hidden="true" />
                  <span className="rail-products__name">{p.name}</span>
                </Link>
              </li>
            );
          })}
        </ul>
      )}

      {/* "+ Product" CTA — the prominent create action, sitting where the old
          "+ Direct" rail button was (Direct is now the omnipresent FAB). */}
      <button
        type="button"
        className="rail-products__cta"
        onClick={() => setCreating(true)}
        title={t("newProduct")}
      >
        <PlusIcon />
        <span>{t("newProduct")}</span>
      </button>

      {/* CREATE — a native <dialog> hosting the real create form. */}
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
    </section>
  );
}
