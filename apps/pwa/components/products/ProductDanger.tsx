"use client";

import { deleteProduct } from "@/lib/api/products";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useState } from "react";

type DeleteState = "idle" | "confirming" | "deleting" | "error";

/**
 * Product "danger zone" — delete this product. The founder accumulates finished
 * / abandoned / smoke-test products with no way to clear them; this removes one
 * so the Products list stays the real ones. Two-step (Delete → Confirm) so it's
 * never a single accidental click; on success it routes back to the list.
 */
export default function ProductDanger({
  productId,
  productName,
}: {
  productId: string;
  productName: string;
}) {
  const t = useTranslations("products");
  const router = useRouter();
  const [state, setState] = useState<DeleteState>("idle");

  async function confirmDelete() {
    setState("deleting");
    try {
      await deleteProduct(productId);
      // Leave the now-gone product's page; the list re-reads on navigation.
      router.push("/products");
      router.refresh();
    } catch {
      setState("error");
    }
  }

  return (
    <section className="product-danger" aria-label={t("danger.heading")}>
      <h2 className="section-label">{t("danger.heading")}</h2>
      {state === "idle" ? (
        <button
          type="button"
          className="product-danger__delete"
          onClick={() => setState("confirming")}
        >
          {t("danger.delete")}
        </button>
      ) : (
        <div className="product-danger__confirm">
          <p className="product-danger__warn">{t("danger.confirmLine", { name: productName })}</p>
          <div className="product-danger__actions">
            <button
              type="button"
              className="product-danger__cancel"
              onClick={() => setState("idle")}
              disabled={state === "deleting"}
            >
              {t("danger.cancel")}
            </button>
            <button
              type="button"
              className="product-danger__confirm-delete"
              onClick={confirmDelete}
              disabled={state === "deleting"}
            >
              {state === "deleting" ? t("danger.deleting") : t("danger.confirmDelete")}
            </button>
          </div>
          {state === "error" && (
            <p className="product-danger__error" aria-live="polite">
              {t("danger.error")}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
