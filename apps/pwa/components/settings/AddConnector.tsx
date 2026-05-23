"use client";

import type { ConnectorCreate, ConnectorCreated, ConnectorName } from "@/lib/api/types";
import { KNOWN_CONNECTORS } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useState } from "react";
import CopyField from "./CopyField";

type FormState = "idle" | "submitting" | "error";

/**
 * The "Add connector" form. A small calm form: pick a connector, paste the
 * signing secret, optionally set a reference label and a JSON delivery_config
 * (founder-set outbound routing, e.g. notion `{"parent_page_id":"…"}` — never
 * derived from work output).
 *
 * On a successful create the 201 carries the one-time `webhook_url` +
 * `webhook_token` — a capability the server will never show again. We surface
 * it PROMINENTLY with copy affordances and a clear "you won't see this again"
 * note, and only dismiss it on the founder's explicit "Done" so a refresh or
 * stray click can't lose it. `onCreated` re-reads the list underneath.
 *
 * `createConnector` is injected (defaults to the real client) so the surface is
 * unit-testable against a mocked fetch without monkey-patching the module.
 *
 * Additive props for the catalog Connect flow: `initialConnector` pre-selects
 * the picker (so "Connect" on a card opens this panel already pointed at that
 * service — the founder can still change it), and `onCancel`, when present,
 * renders a Cancel control so the panel can be dismissed (it's hosted in a
 * modal). Both default to the standalone form's prior behaviour.
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
  const [secret, setSecret] = useState("");
  const [externalRef, setExternalRef] = useState("");
  const [deliveryConfig, setDeliveryConfig] = useState("");
  const [state, setState] = useState<FormState>("idle");
  const [created, setCreated] = useState<ConnectorCreated | null>(null);
  // A dedicated flag so an invalid-JSON error reads differently from a failed
  // request — both keep the form filled and re-submittable.
  const [configError, setConfigError] = useState(false);
  const t = useTranslations("settings.connectors.form");

  const secretReady = secret.trim().length > 0;

  function reset() {
    setSecret("");
    setExternalRef("");
    setDeliveryConfig("");
    setState("idle");
  }

  async function submit() {
    if (state === "submitting" || !secretReady) return;

    let parsedConfig: Record<string, unknown> = {};
    const raw = deliveryConfig.trim();
    if (raw.length > 0) {
      try {
        const parsed = JSON.parse(raw);
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
          throw new Error("not an object");
        }
        parsedConfig = parsed as Record<string, unknown>;
      } catch {
        setState("error");
        setConfigError(true);
        return;
      }
    }
    setConfigError(false);

    setState("submitting");
    try {
      const result = await createConnector({
        connector,
        signing_secret: secret,
        external_ref: externalRef,
        delivery_config: parsedConfig,
      });
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
            onChange={(e) => setConnector(e.target.value as ConnectorName)}
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

      <label className="connector-form__field">
        <span className="connector-form__label">{t("signingSecret")}</span>
        <input
          className="connector-form__input"
          type="password"
          autoComplete="off"
          placeholder={t("signingSecretPlaceholder")}
          value={secret}
          disabled={state === "submitting"}
          onChange={(e) => setSecret(e.target.value)}
        />
      </label>

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
          disabled={state === "submitting" || !secretReady}
        >
          {state === "submitting" ? t("adding") : t("addConnector")}
        </button>
      </div>
    </form>
  );
}
