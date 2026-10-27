"use client";

/**
 * CredentialRotator — "Replace token" for a token-connected connector (the
 * OAuth connectors' "Reconnect" has its own control). Swaps ONLY the secret on
 * the same connector row: its product bindings, approvers and chat config are
 * untouched, which revoke + re-create could never promise.
 *
 * The token is typed into a password field, sent once, and cleared — it is
 * never rendered back. A 422 means the provider rejected the token (nothing was
 * changed); anything else is a generic retry note.
 *
 * `rotateCredential` is injected (default to the real client) for unit testability.
 */

import { ApiError } from "@/lib/api/client";
import { rotateConnectorCredential } from "@/lib/api/connectors";
import type { Connector, ConnectorCredentialRotated } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useState } from "react";

type RotateState =
  | "closed"
  | "editing"
  | "saving"
  | "done"
  | "webhook-failed"
  | "rejected"
  | "error";

export function CredentialRotator({
  connector,
  onRotated,
  rotateCredential = rotateConnectorCredential,
}: {
  connector: Connector;
  /** Re-read hook so the parent list reflects the rotated row (optional). */
  onRotated?: () => void;
  rotateCredential?: (id: string, signingSecret: string) => Promise<ConnectorCredentialRotated>;
}) {
  const t = useTranslations("settings.connectors.row");
  const [state, setState] = useState<RotateState>("closed");
  const [secret, setSecret] = useState("");
  const fieldId = `rotate-token-${connector.id}`;

  async function save() {
    if (state === "saving" || secret.trim().length === 0) return;
    setState("saving");
    try {
      const result = await rotateCredential(connector.id, secret.trim());
      setSecret("");
      setState(result.webhook_registered === false ? "webhook-failed" : "done");
      onRotated?.();
    } catch (err) {
      setState(err instanceof ApiError && err.status === 422 ? "rejected" : "error");
    }
  }

  if (state === "closed" || state === "done" || state === "webhook-failed") {
    return (
      <div className="connector-card__rotate">
        <button type="button" className="connector-card__ghost" onClick={() => setState("editing")}>
          {t("rotate")}
        </button>
        {state === "done" && (
          <span className="connector-card__approvers-ok" aria-live="polite">
            {t("rotated")}
          </span>
        )}
        {state === "webhook-failed" && (
          <span className="connector-card__error" role="alert">
            {t("rotatedWebhookFailed")}
          </span>
        )}
      </div>
    );
  }

  return (
    <div className="connector-card__rotate">
      <label className="connector-card__approvers-label" htmlFor={fieldId}>
        {t("rotateHeading")}
      </label>
      <p className="connector-card__approvers-hint" id={`${fieldId}-hint`}>
        {t("rotateHint")}
      </p>
      <input
        id={fieldId}
        className="connector-card__approvers-scope"
        type="password"
        autoComplete="off"
        aria-describedby={`${fieldId}-hint`}
        placeholder={t("rotatePlaceholder")}
        value={secret}
        onChange={(e) => {
          setSecret(e.target.value);
          if (state !== "editing" && state !== "saving") setState("editing");
        }}
      />
      <div className="connector-card__approvers-actions">
        <button
          type="button"
          className="connector-card__approvers-save"
          onClick={save}
          disabled={state === "saving" || secret.trim().length === 0}
        >
          {state === "saving" ? t("rotating") : t("rotateSave")}
        </button>
        <button
          type="button"
          className="connector-card__ghost"
          onClick={() => {
            setSecret("");
            setState("closed");
          }}
          disabled={state === "saving"}
        >
          {t("cancel")}
        </button>
        {state === "rejected" && (
          <span className="connector-card__approvers-error" role="alert">
            {t("rotateRejected")}
          </span>
        )}
        {state === "error" && (
          <span className="connector-card__approvers-error" role="alert">
            {t("rotateError")}
          </span>
        )}
      </div>
    </div>
  );
}
