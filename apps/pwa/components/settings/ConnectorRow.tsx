"use client";

import type { Connector, ConnectorImportResult, ConnectorName } from "@/lib/api/types";
import { isImportableConnector } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { ConnectorOAuthButton } from "./ConnectorOAuthButton";
import { GithubAppSetup } from "./GithubAppSetup";
import { isOAuthConnector } from "./connector-fields";

type RowState = "idle" | "confirming" | "revoking" | "importing" | "import-error" | "error";

/**
 * One connected connector, rendered as a CONNECTED catalog card. Shows the
 * connector name, its reference label (if any), the masked token hint (last 4 —
 * never the full capability), a "Connected" status pill, and any delivery_config
 * keys (the outbound routing the founder set).
 *
 * Three actions:
 *  - Import now (Lift B) — REAL, but ONLY when the connector is inbound or
 *    both AND has a bulk-import action (isImportableConnector). Fires
 *    `POST /api/v1/connectors/{id}/import`. Shows inline status (importing /
 *    done / error). On success, surfaces the imported count + last-imported
 *    timestamp and re-reads the list so the row reflects the new state.
 *  - Configure → present per the catalog design but DISABLED (there is NO
 *    backend update endpoint yet); a `title` makes the "coming soon" reason
 *    discoverable without a tooltip-only hint.
 *  - Revoke → REAL, confirm-gated: the first "Revoke" click reveals a "Confirm
 *    revoke" (+ "Cancel") so a soft-revoke is never a single stray tap. Confirm
 *    fires the DELETE; on success `onRevoked` re-reads the list so the card
 *    reflects the new state. A failed revoke shows a calm inline note and keeps
 *    the card actionable — it never crashes the surface.
 *
 * `revoke` and `triggerImport` are injected (default to the real client) for
 * unit testability.
 */
export default function ConnectorRow({
  connector,
  onRevoked,
  onImported,
  revoke,
  triggerImport,
}: {
  connector: Connector;
  onRevoked: () => void;
  onImported?: () => void;
  revoke: (id: string) => Promise<void>;
  triggerImport?: (id: string) => Promise<ConnectorImportResult>;
}) {
  const [state, setState] = useState<RowState>("idle");
  const [lastImport, setLastImport] = useState<ConnectorImportResult | null>(null);
  const t = useTranslations("settings.connectors.row");
  const tConnectors = useTranslations("settings.connectors");

  const showImport =
    connector.is_active &&
    triggerImport !== undefined &&
    isImportableConnector(connector.connector);

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

  async function runImport() {
    if (state === "importing" || !triggerImport) return;
    setState("importing");
    try {
      const result = await triggerImport(connector.id);
      setLastImport(result);
      setState("idle");
      onImported?.();
    } catch {
      setState("import-error");
    }
  }

  const configKeys = Object.keys(connector.delivery_config ?? {});
  // "Last imported" — prefer the just-completed import (fresh in memory)
  // over the row's stored value, so the success state is reflected
  // immediately without waiting for the list refetch to land.
  const lastImportAt = lastImport?.last_import_at ?? connector.last_import_at;
  const lastImportCount = lastImport?.imported_count ?? connector.last_import_count;

  return (
    <li className="connector-card connector-card--connected">
      <div className="connector-card__body">
        <div className="connector-card__head">
          <span className="connector-card__name">{connector.connector}</span>
          {/* Lift E46 — needs_reauth flips the pill to a calm warning so the
              founder can tell a working binding apart from a binding whose
              OAuth token is silently dead. */}
          {!connector.is_active ? (
            <span className="connector-card__pill connector-card__pill--revoked">
              {t("revoked")}
            </span>
          ) : connector.needs_reauth ? (
            <span
              className="connector-card__pill connector-card__pill--needs-reauth"
              data-testid="connector-pill-needs-reauth"
            >
              {t("needs_reauth")}
            </span>
          ) : (
            <span className="connector-card__pill connector-card__pill--connected">
              {tConnectors("connected")}
            </span>
          )}
        </div>
        {isOAuthConnector(connector.connector as ConnectorName) ? (
          <div className="connector-card__oauth">
            {/* A binding already exists → show Connect / Connected. github uses
                the App-aware control (configured, no probe — a binding implies
                the App exists); other OAuth connectors use the plain button.
                Lift E46 — when the bound token needs re-auth, the inner
                control hides the steady "Connected as" chip and surfaces a
                "Reconnect with X" CTA instead. */}
            {connector.connector === "github" ? (
              <GithubAppSetup
                configured
                connectedLabel={connector.oauth_account_label}
                needsReauth={connector.needs_reauth}
              />
            ) : (
              <ConnectorOAuthButton
                provider={connector.connector}
                connectedLabel={connector.oauth_account_label}
                needsReauth={connector.needs_reauth}
              />
            )}
          </div>
        ) : null}
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
        {lastImportAt !== null && lastImportAt !== undefined ? (
          <p className="connector-card__import-stamp" aria-live="polite">
            {t("lastImported", {
              count: lastImportCount ?? 0,
              at: lastImportAt,
            })}
          </p>
        ) : null}
      </div>

      <div className="connector-card__actions">
        {state === "error" && (
          <span className="connector-card__error" aria-live="polite">
            {t("revokeError")}
          </span>
        )}
        {state === "import-error" && (
          <span className="connector-card__error" aria-live="polite">
            {t("importError")}
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
            {showImport ? (
              <button
                type="button"
                className="connector-card__import"
                onClick={runImport}
                disabled={state === "importing"}
              >
                {state === "importing" ? t("importing") : t("importNow")}
              </button>
            ) : null}
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
