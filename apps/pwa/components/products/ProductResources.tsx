"use client";

import {
  addResource as realAddResource,
  listResources as realListResources,
  removeResource as realRemoveResource,
} from "@/lib/api/resources";
import type { ProductResource, ProductResourceCreate } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import AddResourceForm from "./AddResourceForm";

/**
 * "Resources" — the product's named pointers (repo / doc / deploy / note). Each
 * row shows the title, a kind chip, an external link when the resource carries a
 * URL, and a quiet remove affordance. An "Add resource" button opens the add
 * form in a native <dialog> (same pattern as the rail's New-product modal); a
 * successful add re-reads the list. A calm empty state when there are none, and
 * a calm inline note (never a crash) when the read fails.
 *
 * The list/mutate clients are injected (defaulting to the real ones) so the
 * surface is unit-testable against mocks — mirrors NewProductForm.
 */
type ListState = { data: ProductResource[]; failed: boolean } | null;

export default function ProductResources({
  productId,
  listResources = realListResources,
  addResource = realAddResource,
  removeResource = realRemoveResource,
}: {
  productId: string;
  listResources?: (productId: string) => Promise<ProductResource[]>;
  addResource?: (productId: string, input: ProductResourceCreate) => Promise<ProductResource>;
  removeResource?: (productId: string, resourceId: string) => Promise<void>;
}) {
  const [list, setList] = useState<ListState>(null);
  const [adding, setAdding] = useState(false);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const t = useTranslations("products.resources");

  async function load() {
    try {
      setList({ data: await listResources(productId), failed: false });
    } catch {
      setList({ data: [], failed: true });
    }
  }

  useEffect(() => {
    let active = true;
    listResources(productId)
      .then((data) => active && setList({ data, failed: false }))
      .catch(() => active && setList({ data: [], failed: true }));
    return () => {
      active = false;
    };
  }, [productId, listResources]);

  // Drive the native <dialog> from `adding`. showModal() gives the backdrop,
  // focus trap, and Escape-to-close for free; the try/catch keeps it safe in
  // jsdom (where showModal() throws) by falling back to the `open` attribute so
  // the form stays in the accessibility tree under test.
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    try {
      if (adding && !dialog.open) dialog.showModal();
      if (!adding && dialog.open) dialog.close();
    } catch {
      dialog.open = adding;
    }
  }, [adding]);

  async function remove(resourceId: string) {
    try {
      await removeResource(productId, resourceId);
    } finally {
      load();
    }
  }

  const resources = list && !list.failed ? list.data : [];

  return (
    <section className="product-resources" aria-label={t("heading")}>
      <header className="product-resources__head">
        <h2 className="section-label">{t("heading")}</h2>
        <button type="button" className="product-resources__add" onClick={() => setAdding(true)}>
          {t("add")}
        </button>
      </header>

      {list === null ? (
        <p className="product-resources__note" aria-busy="true">
          {t("loading")}
        </p>
      ) : list.failed ? (
        <p className="product-resources__note" aria-live="polite">
          {t("loadError")}
        </p>
      ) : resources.length === 0 ? (
        <p className="product-resources__empty">{t("empty")}</p>
      ) : (
        <ul className="product-resources__list">
          {resources.map((r) => (
            <li key={r.id} className="product-resources__row">
              <span className="product-resources__chip" aria-hidden="true">
                {r.kind}
              </span>
              <div className="product-resources__body">
                <span className="product-resources__title">{r.title}</span>
                {r.url && (
                  <a
                    className="product-resources__link"
                    href={r.url}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    {t("open")}
                  </a>
                )}
                {r.note && <span className="product-resources__note-line">{r.note}</span>}
              </div>
              <button
                type="button"
                className="product-resources__remove"
                onClick={() => remove(r.id)}
                title={t("remove")}
                aria-label={t("remove")}
              >
                {t("remove")}
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* ADD — a native <dialog> hosting the real add form. */}
      <dialog
        ref={dialogRef}
        className="add-resource-modal"
        aria-label={t("add")}
        onClose={() => setAdding(false)}
      >
        {adding ? (
          <div className="add-resource-modal__panel">
            <p className="add-resource-modal__title">{t("add")}</p>
            <AddResourceForm
              productId={productId}
              addResource={addResource}
              onAdded={() => {
                setAdding(false);
                load();
              }}
              onCancel={() => setAdding(false)}
            />
          </div>
        ) : null}
      </dialog>
    </section>
  );
}
