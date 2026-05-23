"use client";

import type { Connector } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useState } from "react";

type RowState = "idle" | "confirming" | "revoking" | "error";

/**
 * One connected connector, rendered as a CONNECTED catalog card. Shows the
 * connector name, its reference label (if any), the masked token hint (last 4 —
 * never the full capability), a "Connected" status pill, and any delivery_config
 * keys (the outbound routing the founder set).
 *
 * Two actions:
 *  - Configure → present per the catalog design but DISABLED (there is NO
 *    backend update endpoint yet); a `title` makes the "coming soon" reason
 *    discoverable without a tooltip-only hint.
 *  - Revoke → REAL, confirm-gated: the first "Revoke" click reveals a "Confirm
 *    revoke" (+ "Cancel") so a soft-revoke is never a single stray tap. Confirm
 *    fires the DELETE; on success `onRevoked` re-reads the list so the card
 *    reflects the new state. A failed revoke shows a calm inline note and keeps
 *    the card actionable — it never crashes the surface.
 *
 * `revoke` is injected (defaults to the real client) for unit testability.
 */
export default function ConnectorRow({
  connector,
  onRevoked,
  revoke,
}: {
  connector: Connector;
  onRevoked: () => void;
  revoke: (id: string) => Promise<void>;
}) {
  const [state, setState] = useState<RowState>("idle");
  const t = useTranslations("settings.connectors.row");
  const tConnectors = useTranslations("settings.connectors");

  async function confirmRevoke() {
    if (state === "revoking") return;
    setState("revoking");
    try {
      await revoke(connector.id);
      onRevoked();
      // The container re-read will replace this card; leave it in revoking until
      // then so the button can't be re-fired.
    } catch {
      setState("error");
    }
  }

  const configKeys = Object.keys(connector.delivery_config ?? {});

  return (
    <li className="connector-card connector-card--connected">
      <div className="connector-card__body">
        <div className="connector-card__head">
          <span className="connector-card__name">{connector.connector}</span>
          {connector.is_active ? (
            <span className="connector-card__pill connector-card__pill--connected">
              {tConnectors("connected")}
            </span>
          ) : (
            <span className="connector-card__pill connector-card__pill--revoked">
              {t("revoked")}
            </span>
          )}
        </div>
        <p className="connector-card__detail">
          {connector.external_ref ? (
            <span className="connector-card__ref">{connector.external_ref}</span>
          ) : null}
          {configKeys.length > 0 ? (
            <span className="connector-card__config">
              {t("deliversOut", { keys: configKeys.join(", ") })}
            </span>
          ) : null}
          <span className="connector-card__hint" title={t("tokenHintTitle")}>
            {connector.token_hint}
          </span>
        </p>
      </div>

      <div className="connector-card__actions">
        {state === "error" && (
          <span className="connector-card__error" aria-live="polite">
            {t("revokeError")}
          </span>
        )}

        {state === "confirming" || state === "revoking" ? (
          <>
            <button
              type="button"
              className="connector-card__danger"
              onClick={confirmRevoke}
              disabled={state === "revoking"}
            >
              {state === "revoking" ? t("revoking") : t("confirmRevoke")}
            </button>
            <button
              type="button"
              className="connector-card__ghost"
              onClick={() => setState("idle")}
              disabled={state === "revoking"}
            >
              {t("cancel")}
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              className="connector-card__ghost"
              disabled
              title={t("configureTitle")}
            >
              {t("configure")}
            </button>
            {connector.is_active ? (
              <button
                type="button"
                className="connector-card__revoke"
                onClick={() => setState("confirming")}
              >
                {t("revoke")}
              </button>
            ) : null}
          </>
        )}
      </div>
    </li>
  );
}
