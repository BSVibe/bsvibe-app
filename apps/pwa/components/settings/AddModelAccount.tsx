"use client";

import type { ModelAccount, ModelAccountCreate } from "@/lib/api/types";
import { useState } from "react";

type FormState = "idle" | "submitting" | "error" | "success";

/**
 * The "Add model account" form. A small calm form: name the provider + the
 * litellm model identifier, give it a label, and paste the API key/credential.
 * The key is a secret — like a connector token it
 * is sent ONCE and never read back: on success we confirm the account was added
 * (with its label) and clear the key field rather than echoing it.
 *
 * `onCreated` re-reads the list underneath. `createAccount` is injected
 * (defaults to the real client at the call site) so the surface is unit-testable
 * against a mocked fetch without monkey-patching the module.
 */
export default function AddModelAccount({
  onCreated,
  createAccount,
}: {
  onCreated: () => void;
  createAccount: (input: ModelAccountCreate) => Promise<ModelAccount>;
}) {
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [label, setLabel] = useState("");
  const [apiBase, setApiBase] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [state, setState] = useState<FormState>("idle");
  // The label of the just-created account, surfaced in the success note — we
  // confirm WHAT was added without ever echoing the secret.
  const [createdLabel, setCreatedLabel] = useState("");

  const ready =
    provider.trim().length > 0 &&
    model.trim().length > 0 &&
    label.trim().length > 0 &&
    apiKey.trim().length > 0;

  function reset() {
    setProvider("");
    setModel("");
    setLabel("");
    setApiBase("");
    setApiKey("");
  }

  async function submit() {
    if (state === "submitting" || !ready) return;

    setState("submitting");
    try {
      const created = await createAccount({
        provider: provider.trim(),
        label: label.trim(),
        litellm_model: model.trim(),
        api_key: apiKey,
        api_base: apiBase,
      });
      setCreatedLabel(created.label);
      reset();
      setState("success");
      onCreated();
    } catch {
      setState("error");
    }
  }

  return (
    <form
      className="account-form"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <div className="account-form__row">
        <label className="account-form__field">
          <span className="account-form__label">Provider</span>
          <input
            className="account-form__input"
            type="text"
            placeholder="e.g. openai, anthropic, ollama"
            value={provider}
            disabled={state === "submitting"}
            onChange={(e) => setProvider(e.target.value)}
          />
        </label>

        <label className="account-form__field">
          <span className="account-form__label">Model identifier</span>
          <input
            className="account-form__input"
            type="text"
            placeholder="e.g. gpt-5, claude-sonnet-4, ollama/qwen3"
            value={model}
            disabled={state === "submitting"}
            onChange={(e) => setModel(e.target.value)}
          />
        </label>
      </div>

      <label className="account-form__field">
        <span className="account-form__label">Label</span>
        <input
          className="account-form__input"
          type="text"
          placeholder="A name you'll recognise — e.g. Primary"
          value={label}
          disabled={state === "submitting"}
          onChange={(e) => setLabel(e.target.value)}
        />
      </label>

      <label className="account-form__field">
        <span className="account-form__label">API base (optional)</span>
        <input
          className="account-form__input"
          type="text"
          placeholder="Override only for self-hosted / proxy endpoints"
          value={apiBase}
          disabled={state === "submitting"}
          onChange={(e) => setApiBase(e.target.value)}
        />
      </label>

      <label className="account-form__field">
        <span className="account-form__label">API key</span>
        <input
          className="account-form__input"
          type="password"
          autoComplete="off"
          placeholder="The provider credential — stored encrypted, never shown again"
          value={apiKey}
          disabled={state === "submitting"}
          onChange={(e) => setApiKey(e.target.value)}
        />
        <span className="account-form__hint">
          I store this encrypted and never show it back — you won&rsquo;t see it again here.
        </span>
      </label>

      <div className="account-form__foot">
        {state === "error" && (
          <span className="account-form__error" aria-live="polite">
            Couldn&rsquo;t add that model account — check the details and try again.
          </span>
        )}
        {state === "success" && (
          <span className="account-form__success" aria-live="polite">
            Added “{createdLabel}”. The key is stored encrypted — it won&rsquo;t be shown again.
          </span>
        )}
        <button
          type="submit"
          className="account-form__submit"
          disabled={state === "submitting" || !ready}
        >
          {state === "submitting" ? "Adding…" : "Add model account"}
        </button>
      </div>
    </form>
  );
}
