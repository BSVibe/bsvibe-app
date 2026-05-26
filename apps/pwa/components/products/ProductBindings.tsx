"use client";

import { listConnectors as realListConnectors } from "@/lib/api/connectors";
import {
  createBinding as realCreateBinding,
  listBindings as realListBindings,
  removeBinding as realRemoveBinding,
  updateBinding as realUpdateBinding,
} from "@/lib/api/resource-bindings";
import {
  type Connector,
  OUTPUT_MODES,
  type OutputMode,
  type ResourceBinding,
  type ResourceBindingCreate,
  type ResourceBindingUpdate,
} from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

/**
 * "Connector bindings" — the per-Product × ConnectorAccount 3-knob binding
 * surface (Workflow §3). Each row shows the connector + the connector-side
 * `resource_id`, with two minimal controls for the two most-load-bearing
 * knobs:
 *
 *   - `trigger.enabled`  ← checkbox  (off by default — a binding doesn't
 *                                     auto-fire until the founder turns it on)
 *   - `output_mode`      ← select    (`safe` | `direct`; `safe` is the default
 *                                     for non-founder triggers, sending the
 *                                     Deliverable to the Safe Mode queue)
 *
 * `selection.filters` and the JSON-shaped `selection` knob aren't surfaced
 * yet — they are connector-shaped (no one UI fits them all) and B10a's
 * scope is to ship the binding + the two boolean/enum knobs founders flip
 * day-to-day. A future B10c surface can layer in the JSON-shape knobs.
 *
 * The list/mutate clients (+ the connector list for the Add form's
 * dropdown) are injected so the surface is unit-testable against mocks
 * (mirrors ProductResources / NewProductForm).
 */
type ListState =
  | { state: "loading" }
  | { state: "error" }
  | { state: "ready"; rows: ResourceBinding[] };

export default function ProductBindings({
  productId,
  listBindings = realListBindings,
  createBinding = realCreateBinding,
  updateBinding = realUpdateBinding,
  removeBinding = realRemoveBinding,
  listConnectors = realListConnectors,
}: {
  productId: string;
  listBindings?: (productId: string) => Promise<ResourceBinding[]>;
  createBinding?: (productId: string, input: ResourceBindingCreate) => Promise<ResourceBinding>;
  updateBinding?: (
    productId: string,
    bindingId: string,
    patch: ResourceBindingUpdate,
  ) => Promise<ResourceBinding>;
  removeBinding?: (productId: string, bindingId: string) => Promise<void>;
  listConnectors?: () => Promise<Connector[]>;
}) {
  const t = useTranslations("products.bindings");

  const [list, setList] = useState<ListState>({ state: "loading" });
  const [connectors, setConnectors] = useState<Connector[]>([]);
  // Form state for adding a new binding.
  const [adding, setAdding] = useState(false);
  const [formConnectorId, setFormConnectorId] = useState<string>("");
  const [formResourceId, setFormResourceId] = useState<string>("");
  const [formOutputMode, setFormOutputMode] = useState<OutputMode>("safe");
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function load() {
    try {
      const rows = await listBindings(productId);
      setList({ state: "ready", rows });
    } catch {
      setList({ state: "error" });
    }
  }

  useEffect(() => {
    let active = true;
    listBindings(productId)
      .then((rows) => {
        if (active) setList({ state: "ready", rows });
      })
      .catch(() => {
        if (active) setList({ state: "error" });
      });
    return () => {
      active = false;
    };
  }, [productId, listBindings]);

  // Lazy-load the connector list for the Add form's picker the first time the
  // founder opens it — keeps the steady-state surface a single GET.
  useEffect(() => {
    if (!adding) return;
    let active = true;
    listConnectors()
      .then((rows) => {
        if (!active) return;
        setConnectors(rows);
        if (rows.length > 0 && !formConnectorId) setFormConnectorId(rows[0].id);
      })
      .catch(() => {
        /* leave dropdown empty — submit is gated on a chosen connector */
      });
    return () => {
      active = false;
    };
  }, [adding, listConnectors, formConnectorId]);

  const canSubmit = !submitting && formConnectorId.length > 0 && formResourceId.trim().length > 0;

  async function submitAdd() {
    if (!canSubmit) return;
    setSubmitting(true);
    setFormError(null);
    try {
      await createBinding(productId, {
        connector_account_id: formConnectorId,
        resource_id: formResourceId,
        output_mode: formOutputMode,
      });
      // Reset form, close, and re-read.
      setFormResourceId("");
      setFormOutputMode("safe");
      setAdding(false);
      await load();
    } catch {
      setFormError(t("addError"));
    } finally {
      setSubmitting(false);
    }
  }

  async function toggleTrigger(row: ResourceBinding, next: boolean) {
    try {
      await updateBinding(productId, row.id, {
        trigger: { enabled: next, filters: row.trigger.filters },
      });
    } finally {
      load();
    }
  }

  async function setOutputMode(row: ResourceBinding, next: OutputMode) {
    try {
      await updateBinding(productId, row.id, { output_mode: next });
    } finally {
      load();
    }
  }

  async function remove(row: ResourceBinding) {
    try {
      await removeBinding(productId, row.id);
    } finally {
      load();
    }
  }

  return (
    <section className="product-bindings" aria-label={t("heading")}>
      <header className="product-bindings__head">
        <h2 className="section-label">{t("heading")}</h2>
        <button
          type="button"
          className="product-bindings__add"
          onClick={() => setAdding((v) => !v)}
        >
          {adding ? t("cancel") : t("add")}
        </button>
      </header>

      {list.state === "loading" && (
        <p className="product-bindings__note" aria-busy="true">
          {t("loading")}
        </p>
      )}
      {list.state === "error" && (
        <p className="product-bindings__note" aria-live="polite">
          {t("loadError")}
        </p>
      )}
      {list.state === "ready" && list.rows.length === 0 && !adding && (
        <p className="product-bindings__empty">{t("empty")}</p>
      )}
      {list.state === "ready" && list.rows.length > 0 && (
        <ul className="product-bindings__list">
          {list.rows.map((row) => (
            <li key={row.id} className="product-bindings__row">
              <div className="product-bindings__ident">
                <span className="product-bindings__resource">{row.resource_id}</span>
                <span className="product-bindings__connector" aria-hidden="true">
                  {row.connector_account_id?.slice(0, 8) ?? ""}
                </span>
              </div>

              <label className="product-bindings__knob">
                <input
                  type="checkbox"
                  checked={row.trigger.enabled}
                  onChange={(e) => toggleTrigger(row, e.target.checked)}
                  aria-label={t("triggerEnabled")}
                />
                <span>{t("triggerEnabled")}</span>
              </label>

              <label className="product-bindings__knob">
                <span>{t("outputMode")}</span>
                <select
                  value={row.output_mode}
                  onChange={(e) => setOutputMode(row, e.target.value as OutputMode)}
                  aria-label={t("outputMode")}
                >
                  {OUTPUT_MODES.map((m) => (
                    <option key={m} value={m}>
                      {t(`outputModes.${m}`)}
                    </option>
                  ))}
                </select>
              </label>

              <button
                type="button"
                className="product-bindings__remove"
                onClick={() => remove(row)}
                title={t("remove")}
                aria-label={t("remove")}
              >
                {t("remove")}
              </button>
            </li>
          ))}
        </ul>
      )}

      {adding && (
        <form
          className="product-bindings__form"
          aria-label={t("add")}
          onSubmit={(e) => {
            e.preventDefault();
            submitAdd();
          }}
        >
          <div className="product-bindings__field">
            <label htmlFor="binding-connector">{t("form.connector")}</label>
            <select
              id="binding-connector"
              value={formConnectorId}
              onChange={(e) => setFormConnectorId(e.target.value)}
              disabled={submitting}
            >
              {connectors.length === 0 ? (
                <option value="">{t("form.noConnectors")}</option>
              ) : (
                connectors.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.connector}
                    {c.external_ref ? ` — ${c.external_ref}` : ""}
                  </option>
                ))
              )}
            </select>
          </div>

          <div className="product-bindings__field">
            <label htmlFor="binding-resource">{t("form.resourceId")}</label>
            <input
              id="binding-resource"
              type="text"
              value={formResourceId}
              placeholder={t("form.resourceIdPlaceholder")}
              onChange={(e) => setFormResourceId(e.target.value)}
              disabled={submitting}
            />
          </div>

          <div className="product-bindings__field">
            <label htmlFor="binding-output-mode">{t("outputMode")}</label>
            <select
              id="binding-output-mode"
              value={formOutputMode}
              onChange={(e) => setFormOutputMode(e.target.value as OutputMode)}
              disabled={submitting}
            >
              {OUTPUT_MODES.map((m) => (
                <option key={m} value={m}>
                  {t(`outputModes.${m}`)}
                </option>
              ))}
            </select>
          </div>

          {formError && (
            <p className="product-bindings__error" aria-live="polite">
              {formError}
            </p>
          )}

          <div className="product-bindings__form-foot">
            <button
              type="button"
              className="product-bindings__cancel"
              onClick={() => setAdding(false)}
              disabled={submitting}
            >
              {t("cancel")}
            </button>
            <button type="submit" className="product-bindings__submit" disabled={!canSubmit}>
              {submitting ? t("form.adding") : t("form.add")}
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
