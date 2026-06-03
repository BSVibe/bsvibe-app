"use client";

import type { ConnectorCreate, ConnectorCreated, ConnectorName } from "@/lib/api/types";
import { KNOWN_CONNECTORS } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useId, useMemo, useState } from "react";
import CopyField from "./CopyField";
import { type FieldDescriptor, descriptorFor } from "./connector-fields";

type FormState = "idle" | "submitting" | "error";

/**
 * The "Add connector" form. A small calm form: pick a connector, then fill
 * the per-connector binding fields (the descriptor in `connector-fields.ts`
 * declares them). Lift B branches the form per connector:
 *
 *   - outbound connectors (github / slack / telegram / discord / sentry /
 *     email-sender) — webhook `signing_secret` + optional JSON
 *     `delivery_config` (founder-set outbound routing).
 *   - inbound connectors (obsidian / claude / gpt) — per-connector inbound
 *     fields (vault_path / exclude_patterns / default_region for obsidian;
 *     export_path / default_region for claude / gpt). No webhook secret;
 *     a stable placeholder fills the backend's non-empty requirement.
 *   - both (notion) — webhook secret + outbound JSON + optional inbound
 *     block (api_token / database_ids).
 *
 * On a successful create the 201 carries the one-time `webhook_url` +
 * `webhook_token` — a capability the server will never show again. We
 * surface it PROMINENTLY with copy affordances and a clear "you won't see
 * this again" note, and only dismiss it on the founder's explicit "Done"
 * so a refresh or stray click can't lose it. `onCreated` re-reads the list
 * underneath.
 *
 * `createConnector` is injected (defaults to the real client) so the
 * surface is unit-testable against a mocked fetch without monkey-patching
 * the module.
 *
 * Additive props for the catalog Connect flow: `initialConnector`
 * pre-selects the picker (so "Connect" on a card opens this panel already
 * pointed at that service — the founder can still change it), and
 * `onCancel`, when present, renders a Cancel control so the panel can be
 * dismissed (it's hosted in a modal). Both default to the standalone
 * form's prior behaviour.
 */
export default function AddConnector({
  onCreated,
  createConnector,
  initialConnector,
  onCancel,
}: {
  onCreated: () => void;
  createConnector: (input: ConnectorCreate) => Promise<ConnectorCreated>;
  initialConnector?: ConnectorName;
  onCancel?: () => void;
}) {
  const [connector, setConnector] = useState<ConnectorName>(
    initialConnector ?? KNOWN_CONNECTORS[0],
  );
  const [externalRef, setExternalRef] = useState("");
  const [deliveryConfig, setDeliveryConfig] = useState("");
  // Per-connector field values keyed by descriptor.key. Resets on connector
  // change so a half-filled obsidian form doesn't leak into a notion bind.
  const [values, setValues] = useState<Record<string, string>>({});
  const [state, setState] = useState<FormState>("idle");
  const [created, setCreated] = useState<ConnectorCreated | null>(null);
  // A dedicated flag so an invalid-JSON error reads differently from a failed
  // request — both keep the form filled and re-submittable.
  const [configError, setConfigError] = useState(false);
  const t = useTranslations("settings.connectors.form");

  const descriptor = useMemo(() => descriptorFor(connector), [connector]);

  // A field is "ready" when every required field has a non-blank value.
  const allRequiredReady = descriptor.fields
    .filter((f) => f.required)
    .every((f) => (values[f.key] ?? "").trim().length > 0);

  function reset() {
    setValues({});
    setExternalRef("");
    setDeliveryConfig("");
    setState("idle");
  }

  function changeConnector(next: ConnectorName) {
    setConnector(next);
    // Drop any half-filled per-connector inputs — the schema diverges per
    // connector, so cross-leaking values would be confusing at best.
    setValues({});
    setDeliveryConfig("");
    setConfigError(false);
  }

  async function submit() {
    if (state === "submitting" || !allRequiredReady) return;

    // Pre-parse the JSON delivery_config (when the descriptor declares it
    // as visible) so we can surface a structured parse error inline.
    let parsedJson = "";
    if (descriptor.showDeliveryConfigJson) {
      const raw = deliveryConfig.trim();
      if (raw.length > 0) {
        try {
          const parsed = JSON.parse(raw);
          if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
            throw new Error("not an object");
          }
          parsedJson = JSON.stringify(parsed);
        } catch {
          setState("error");
          setConfigError(true);
          return;
        }
      }
    }
    setConfigError(false);

    const packed = descriptor.pack(
      { ...values, deliveryConfigParsed: parsedJson },
      connector,
      externalRef,
    );

    setState("submitting");
    try {
      const result = await createConnector(packed);
      setCreated(result);
      reset();
      onCreated();
    } catch {
      setState("error");
    }
  }

  // After a successful create we show ONLY the one-time secret panel until the
  // founder dismisses it — the form is hidden so the capability is the focus.
  if (created) {
    return (
      <section className="connector-secret" aria-label={t("credentialsLabel")}>
        <p className="connector-secret__title">{t("createdTitle")}</p>
        <p className="connector-secret__warn">{t("createdWarn")}</p>

        <CopyField label={t("webhookUrl")} value={created.webhook_url} />
        <CopyField label={t("webhookToken")} value={created.webhook_token} secret />

        <p className="connector-secret__hint">{t("createdHint", { service: created.connector })}</p>
        <button type="button" className="connector-secret__done" onClick={() => setCreated(null)}>
          {t("done")}
        </button>
      </section>
    );
  }

  const inboundFields = descriptor.fields.filter((f) => f.group === "inbound");
  const ungroupedFields = descriptor.fields.filter((f) => f.group !== "inbound");

  return (
    <form
      className="connector-form"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <div className="connector-form__row">
        <label className="connector-form__field">
          <span className="connector-form__label">{t("connector")}</span>
          <select
            className="connector-form__input"
            value={connector}
            disabled={state === "submitting"}
            onChange={(e) => changeConnector(e.target.value as ConnectorName)}
          >
            {KNOWN_CONNECTORS.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>

        <label className="connector-form__field">
          <span className="connector-form__label">{t("reference")}</span>
          <input
            className="connector-form__input"
            type="text"
            placeholder={t("referencePlaceholder")}
            value={externalRef}
            disabled={state === "submitting"}
            onChange={(e) => setExternalRef(e.target.value)}
          />
        </label>
      </div>

      {ungroupedFields.map((field) => (
        <FieldRow
          key={field.key}
          field={field}
          value={values[field.key] ?? ""}
          disabled={state === "submitting"}
          onChange={(v) => setValues((prev) => ({ ...prev, [field.key]: v }))}
          t={t}
        />
      ))}

      {descriptor.showDeliveryConfigJson ? (
        <label className="connector-form__field">
          <span className="connector-form__label">{t("deliveryConfig")}</span>
          <textarea
            className="connector-form__input connector-form__input--mono"
            rows={2}
            placeholder={t("deliveryConfigPlaceholder")}
            value={deliveryConfig}
            disabled={state === "submitting"}
            onChange={(e) => setDeliveryConfig(e.target.value)}
          />
          <span className="connector-form__hint">{t("deliveryConfigHint")}</span>
        </label>
      ) : null}

      {inboundFields.length > 0 ? (
        <fieldset className="connector-form__group">
          <legend className="connector-form__group-label">
            {t(`inbound.${connector}.heading`)}
          </legend>
          <p className="connector-form__group-hint">{t(`inbound.${connector}.hint`)}</p>
          {inboundFields.map((field) => (
            <FieldRow
              key={field.key}
              field={field}
              value={values[field.key] ?? ""}
              disabled={state === "submitting"}
              onChange={(v) => setValues((prev) => ({ ...prev, [field.key]: v }))}
              t={t}
            />
          ))}
        </fieldset>
      ) : null}

      <div className="connector-form__foot">
        {state === "error" && configError && (
          <span className="connector-form__error" aria-live="polite">
            {t("jsonError")}
          </span>
        )}
        {state === "error" && !configError && (
          <span className="connector-form__error" aria-live="polite">
            {t("createError")}
          </span>
        )}
        {onCancel ? (
          <button
            type="button"
            className="connector-form__cancel"
            onClick={onCancel}
            disabled={state === "submitting"}
          >
            {t("cancel")}
          </button>
        ) : null}
        <button
          type="submit"
          className="connector-form__submit"
          disabled={state === "submitting" || !allRequiredReady}
        >
          {state === "submitting" ? t("adding") : t("addConnector")}
        </button>
      </div>
    </form>
  );
}

function FieldRow({
  field,
  value,
  disabled,
  onChange,
  t,
}: {
  field: FieldDescriptor;
  value: string;
  disabled: boolean;
  onChange: (next: string) => void;
  t: (key: string) => string;
}) {
  const label = t(`fields.${field.i18nKey}.label`);
  const placeholder = t(`fields.${field.i18nKey}.placeholder`);
  const inputId = useId();
  const control =
    field.kind === "textarea" ? (
      <textarea
        id={inputId}
        className="connector-form__input connector-form__input--mono"
        rows={3}
        placeholder={placeholder}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      />
    ) : (
      <input
        id={inputId}
        className="connector-form__input"
        type={field.kind === "password" ? "password" : "text"}
        autoComplete="off"
        placeholder={placeholder}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  return (
    <div className="connector-form__field" data-field={field.key}>
      <label className="connector-form__label" htmlFor={inputId}>
        {label}
        {field.required ? <span className="connector-form__required"> *</span> : null}
      </label>
      {control}
    </div>
  );
}
