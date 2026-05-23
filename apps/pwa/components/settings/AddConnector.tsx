"use client";

import type { ConnectorCreate, ConnectorCreated, ConnectorName } from "@/lib/api/types";
import { KNOWN_CONNECTORS } from "@/lib/api/types";
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
      <section className="connector-secret" aria-label="New connector credentials">
        <p className="connector-secret__title">
          Connector added — here&rsquo;s its webhook. Copy it now.
        </p>
        <p className="connector-secret__warn">
          This is the only time you&rsquo;ll see this. The token is a secret — you won&rsquo;t see
          it again, so copy it before you close this.
        </p>

        <CopyField label="Webhook URL" value={created.webhook_url} />
        <CopyField label="Webhook token" value={created.webhook_token} secret />

        <p className="connector-secret__hint">
          Paste the webhook URL into {created.connector}&rsquo;s outgoing-webhook settings. The
          token is part of the URL — keep it private.
        </p>
        <button type="button" className="connector-secret__done" onClick={() => setCreated(null)}>
          Done — I&rsquo;ve copied it
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
          <span className="connector-form__label">Connector</span>
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
          <span className="connector-form__label">Reference (optional)</span>
          <input
            className="connector-form__input"
            type="text"
            placeholder="e.g. acme/widgets"
            value={externalRef}
            disabled={state === "submitting"}
            onChange={(e) => setExternalRef(e.target.value)}
          />
        </label>
      </div>

      <label className="connector-form__field">
        <span className="connector-form__label">Signing secret</span>
        <input
          className="connector-form__input"
          type="password"
          autoComplete="off"
          placeholder="The service's webhook signing secret"
          value={secret}
          disabled={state === "submitting"}
          onChange={(e) => setSecret(e.target.value)}
        />
      </label>

      <label className="connector-form__field">
        <span className="connector-form__label">Delivery config (optional, JSON)</span>
        <textarea
          className="connector-form__input connector-form__input--mono"
          rows={2}
          placeholder='e.g. {"parent_page_id":"…"} or {"channel":"#updates"}'
          value={deliveryConfig}
          disabled={state === "submitting"}
          onChange={(e) => setDeliveryConfig(e.target.value)}
        />
        <span className="connector-form__hint">
          Where I deliver finished work back out. Leave blank for inbound-only.
        </span>
      </label>

      <div className="connector-form__foot">
        {state === "error" && configError && (
          <span className="connector-form__error" aria-live="polite">
            Delivery config is not valid JSON — fix it or leave it blank.
          </span>
        )}
        {state === "error" && !configError && (
          <span className="connector-form__error" aria-live="polite">
            Couldn&rsquo;t register that connector — check the details and try again.
          </span>
        )}
        {onCancel ? (
          <button
            type="button"
            className="connector-form__cancel"
            onClick={onCancel}
            disabled={state === "submitting"}
          >
            Cancel
          </button>
        ) : null}
        <button
          type="submit"
          className="connector-form__submit"
          disabled={state === "submitting" || !secretReady}
        >
          {state === "submitting" ? "Adding…" : "Add connector"}
        </button>
      </div>
    </form>
  );
}
