"use client";

/**
 * ProviderAppConfig — operator paste-creds form for a vanilla OAuth connector
 * (slack/notion/discord). Those providers have no manifest auto-create, so the
 * operator creates the OAuth app in the provider's console and pastes the
 * client_id + client_secret here. On save the backend stores them encrypted +
 * registers the provider, then `onSaved` proceeds to the connect. (github uses
 * the manifest flow, not this.)
 *
 * `save` is injectable for tests; it defaults to the real client.
 */

import { setProviderAppCredentials } from "@/lib/api/connectors";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

function titleCase(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function ProviderAppConfig({
  provider,
  onSaved,
  onCancel,
  save = setProviderAppCredentials,
}: {
  provider: string;
  onSaved: () => void | Promise<void>;
  onCancel: () => void;
  save?: (provider: string, clientId: string, clientSecret: string) => Promise<unknown>;
}) {
  const t = useTranslations("settings.connectors.providerConfig");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const idField = useId();
  const secretField = useId();

  const ready = clientId.trim().length > 0 && clientSecret.trim().length > 0;

  async function submit() {
    if (!ready || busy) return;
    setBusy(true);
    setError(false);
    try {
      await save(provider, clientId.trim(), clientSecret.trim());
      await onSaved();
    } catch {
      setError(true);
      setBusy(false);
    }
  }

  return (
    <form
      className="connector-app-config"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <p className="connector-app-config__hint">{t("hint", { provider: titleCase(provider) })}</p>
      <label className="connector-form__field" htmlFor={idField}>
        <span className="connector-form__label">{t("clientId")}</span>
        <input
          id={idField}
          className="connector-form__input"
          type="text"
          autoComplete="off"
          value={clientId}
          disabled={busy}
          onChange={(e) => setClientId(e.target.value)}
        />
      </label>
      <label className="connector-form__field" htmlFor={secretField}>
        <span className="connector-form__label">{t("clientSecret")}</span>
        <input
          id={secretField}
          className="connector-form__input"
          type="password"
          autoComplete="off"
          value={clientSecret}
          disabled={busy}
          onChange={(e) => setClientSecret(e.target.value)}
        />
      </label>
      {error ? (
        <span className="connector-form__error" aria-live="polite">
          {t("error")}
        </span>
      ) : null}
      <div className="connector-form__foot">
        <button type="button" className="connector-form__cancel" onClick={onCancel} disabled={busy}>
          {t("cancel")}
        </button>
        <button type="submit" className="connector-form__submit" disabled={busy || !ready}>
          {busy ? t("saving") : t("save")}
        </button>
      </div>
    </form>
  );
}
