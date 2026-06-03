"use client";

import { createProduct as realCreateProduct } from "@/lib/api/products";
import type { Product, ProductCreate } from "@/lib/api/types";
import { isValidSlug, suggestSlug } from "@/lib/products/slug";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useState } from "react";

type FormState = "idle" | "submitting" | "error";

/** Lift A v2 — only the very basic `http(s)://…` shape is enforced here so
 *  the founder can paste a plain GitHub URL without thinking about formatting.
 *  The backend's `ProductCreate` does the strict validation. */
const REPO_URL_PATTERN = /^https?:\/\/.+/i;

/**
 * The "New product" create form. A small calm form: Name + Slug + optional
 * Git repo URL (Lift A v2). The slug is auto-suggested from the Name
 * (lowercase, separators → hyphens, invalid chars stripped — `lib/products/
 * slug.ts`) and editable; once the founder edits it we stop overriding their
 * value. Submit is gated on a backend-valid slug (`^[a-z][a-z0-9-]*$`) and a
 * well-formed `repo_url` (when present) so we never fire a request that the
 * server would 422; a duplicate slug 409s and surfaces as a calm inline error
 * that keeps the form usable.
 *
 * When `repo_url` is set, the backend kicks off the bootstrap job in the
 * background — the founder lands on the product detail page where
 * `<BootstrapStatusPanel />` shows progress in calm prose.
 *
 * On a successful create we navigate to the new product's `/products/{slug}`
 * and call `onCreated` (the rail re-reads its list underneath).
 *
 * `createProduct` is injected (defaults to the real client) so the surface is
 * unit-testable against a mock without monkey-patching the module.
 */
export default function NewProductForm({
  onCreated,
  onCancel,
  createProduct = realCreateProduct,
}: {
  onCreated: () => void;
  onCancel?: () => void;
  createProduct?: (input: ProductCreate) => Promise<Product>;
}) {
  const router = useRouter();
  const t = useTranslations("shell.products.form");

  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [repoUrl, setRepoUrl] = useState("");
  // True until the founder edits the slug by hand — while true we keep the slug
  // mirrored to the auto-suggestion derived from the Name.
  const [slugAuto, setSlugAuto] = useState(true);
  const [state, setState] = useState<FormState>("idle");
  // A dedicated flag so an invalid-slug error reads differently from a failed
  // request — both keep the form filled and re-submittable.
  const [slugError, setSlugError] = useState(false);
  const [repoUrlError, setRepoUrlError] = useState(false);

  function onNameChange(value: string) {
    setName(value);
    if (slugAuto) setSlug(suggestSlug(value));
  }

  function onSlugChange(value: string) {
    setSlugAuto(false);
    setSlug(value);
  }

  const nameReady = name.trim().length > 0;

  async function submit() {
    if (state === "submitting" || !nameReady) return;

    if (!isValidSlug(slug)) {
      setState("error");
      setSlugError(true);
      return;
    }
    setSlugError(false);

    const trimmedRepo = repoUrl.trim();
    if (trimmedRepo && !REPO_URL_PATTERN.test(trimmedRepo)) {
      setState("error");
      setRepoUrlError(true);
      return;
    }
    setRepoUrlError(false);

    setState("submitting");
    try {
      const payload: ProductCreate = { name: name.trim(), slug };
      if (trimmedRepo) payload.repo_url = trimmedRepo;
      const created = await createProduct(payload);
      onCreated();
      router.push(`/products/${created.slug}`);
    } catch {
      setState("error");
    }
  }

  return (
    <form
      className="new-product-form"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <div className="new-product-form__field">
        <label className="new-product-form__label" htmlFor="new-product-name">
          {t("name")}
        </label>
        <input
          id="new-product-name"
          className="new-product-form__input"
          type="text"
          placeholder={t("namePlaceholder")}
          value={name}
          disabled={state === "submitting"}
          onChange={(e) => onNameChange(e.target.value)}
        />
      </div>

      <div className="new-product-form__field">
        <label className="new-product-form__label" htmlFor="new-product-slug">
          {t("slug")}
        </label>
        <input
          id="new-product-slug"
          className="new-product-form__input new-product-form__input--mono"
          type="text"
          placeholder={t("slugPlaceholder")}
          value={slug}
          disabled={state === "submitting"}
          onChange={(e) => onSlugChange(e.target.value)}
        />
        <span className="new-product-form__hint">{t("slugHint")}</span>
      </div>

      <div className="new-product-form__field">
        <label className="new-product-form__label" htmlFor="new-product-repo-url">
          {t("repoUrl")}
        </label>
        <input
          id="new-product-repo-url"
          className="new-product-form__input new-product-form__input--mono"
          type="text"
          placeholder={t("repoUrlPlaceholder")}
          value={repoUrl}
          disabled={state === "submitting"}
          onChange={(e) => setRepoUrl(e.target.value)}
        />
        <span className="new-product-form__hint">{t("repoUrlHint")}</span>
      </div>

      <div className="new-product-form__foot">
        {state === "error" && slugError && (
          <span className="new-product-form__error" aria-live="polite">
            {t("slugError")}
          </span>
        )}
        {state === "error" && repoUrlError && !slugError && (
          <span className="new-product-form__error" aria-live="polite">
            {t("repoUrlError")}
          </span>
        )}
        {state === "error" && !slugError && !repoUrlError && (
          <span className="new-product-form__error" aria-live="polite">
            {t("createError")}
          </span>
        )}
        {onCancel ? (
          <button
            type="button"
            className="new-product-form__cancel"
            onClick={onCancel}
            disabled={state === "submitting"}
          >
            {t("cancel")}
          </button>
        ) : null}
        <button
          type="submit"
          className="new-product-form__submit"
          disabled={state === "submitting" || !nameReady}
        >
          {state === "submitting" ? t("creating") : t("create")}
        </button>
      </div>
    </form>
  );
}
