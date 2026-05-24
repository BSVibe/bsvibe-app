"use client";

import { addResource as realAddResource } from "@/lib/api/resources";
import { type ProductResource, type ProductResourceCreate, RESOURCE_KINDS } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useState } from "react";

type FormState = "idle" | "submitting" | "error";

/**
 * The "Add resource" form — a small calm form: Type, Title, optional URL,
 * optional Note. Submit is gated on a non-blank Title so we never fire a request
 * the backend would 422; a failed add surfaces as a calm inline error that keeps
 * the form usable. On a successful add we call `onAdded` (the section re-reads
 * its list underneath).
 *
 * `addResource` is injected (defaults to the real client) so the surface is
 * unit-testable against a mock without monkey-patching the module — mirrors
 * NewProductForm / AddConnector.
 */
export default function AddResourceForm({
  productId,
  onAdded,
  onCancel,
  addResource = realAddResource,
}: {
  productId: string;
  onAdded: () => void;
  onCancel?: () => void;
  addResource?: (productId: string, input: ProductResourceCreate) => Promise<ProductResource>;
}) {
  const t = useTranslations("products.resources.form");

  const [kind, setKind] = useState<string>(RESOURCE_KINDS[0]);
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState("");
  const [note, setNote] = useState("");
  const [state, setState] = useState<FormState>("idle");

  const titleReady = title.trim().length > 0;

  async function submit() {
    if (state === "submitting" || !titleReady) return;
    setState("submitting");
    try {
      await addResource(productId, { kind, title, url, note });
      onAdded();
    } catch {
      setState("error");
    }
  }

  return (
    <form
      className="add-resource-form"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <div className="add-resource-form__field">
        <label className="add-resource-form__label" htmlFor="add-resource-kind">
          {t("kind")}
        </label>
        <select
          id="add-resource-kind"
          className="add-resource-form__input"
          value={kind}
          disabled={state === "submitting"}
          onChange={(e) => setKind(e.target.value)}
        >
          {RESOURCE_KINDS.map((k) => (
            <option key={k} value={k}>
              {t(`kinds.${k}`)}
            </option>
          ))}
        </select>
      </div>

      <div className="add-resource-form__field">
        <label className="add-resource-form__label" htmlFor="add-resource-title">
          {t("title")}
        </label>
        <input
          id="add-resource-title"
          className="add-resource-form__input"
          type="text"
          placeholder={t("titlePlaceholder")}
          value={title}
          disabled={state === "submitting"}
          onChange={(e) => setTitle(e.target.value)}
        />
      </div>

      <div className="add-resource-form__field">
        <label className="add-resource-form__label" htmlFor="add-resource-url">
          {t("url")}
        </label>
        <input
          id="add-resource-url"
          className="add-resource-form__input"
          type="url"
          placeholder={t("urlPlaceholder")}
          value={url}
          disabled={state === "submitting"}
          onChange={(e) => setUrl(e.target.value)}
        />
        <span className="add-resource-form__hint">{t("urlHint")}</span>
      </div>

      <div className="add-resource-form__field">
        <label className="add-resource-form__label" htmlFor="add-resource-note">
          {t("note")}
        </label>
        <input
          id="add-resource-note"
          className="add-resource-form__input"
          type="text"
          placeholder={t("notePlaceholder")}
          value={note}
          disabled={state === "submitting"}
          onChange={(e) => setNote(e.target.value)}
        />
      </div>

      <div className="add-resource-form__foot">
        {state === "error" && (
          <span className="add-resource-form__error" aria-live="polite">
            {t("addError")}
          </span>
        )}
        {onCancel ? (
          <button
            type="button"
            className="add-resource-form__cancel"
            onClick={onCancel}
            disabled={state === "submitting"}
          >
            {t("cancel")}
          </button>
        ) : null}
        <button
          type="submit"
          className="add-resource-form__submit"
          disabled={state === "submitting" || !titleReady}
        >
          {state === "submitting" ? t("adding") : t("add")}
        </button>
      </div>
    </form>
  );
}
