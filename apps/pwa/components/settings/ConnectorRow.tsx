"use client";

import type { Connector, ConnectorImportResult } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { ApproversEditor } from "./ApproversEditor";
import { ConnectorOAuthButton } from "./ConnectorOAuthButton";
import { GithubAppSetup } from "./GithubAppSetup";
import { isOAuthConnector } from "./connector-fields";

/** Connectors whose interactive Approve/Reject cards authorize the tapping user
 *  against `delivery_config.authorized_user_ids` — so they get the inline
 *  approvers editor. Telegram authorizes by `chat_id` (not a user list), so it
 *  is deliberately excluded. */
const APPROVER_LIST_CONNECTORS = new Set(["slack", "discord"]);

type RowState = "idle" | "confirming" | "revoking" | "importing" | "import-error" | "error";

/**
 * One connected connector, rendered as a CONNECTED catalog card. Shows the
 * connector name, its reference label (if any), the masked token hint (last 4 —
 * never the full capability), and a "Connected" status pill.
 *
 * Three actions:
 *  - Import now (Lift B) — REAL, but ONLY when the connector exposes a
 *    bulk-import action (the catalog's `importable` flag, carried on the row).
 *    Fires `POST /api/v1/connectors/{id}/import`. Shows inline status (importing /
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
  onUpdated,
  revoke,
  triggerImport,
  updateConnector,
}: {
  connector: Connector;
  onRevoked: () => void;
  onImported?: () => void;
  onUpdated?: () => void;
  revoke: (id: string) => Promise<void>;
  triggerImport?: (id: string) => Promise<ConnectorImportResult>;
  updateConnector?: (
    id: string,
    patch: { delivery_config: Record<string, unknown> },
  ) => Promise<Connector>;
}) {
  const [state, setState] = useState<RowState>("idle");
  const [lastImport, setLastImport] = useState<ConnectorImportResult | null>(null);
  const t = useTranslations("settings.connectors.row");
  const tConnectors = useTranslations("settings.connectors");

  const showImport = connector.is_active && triggerImport !== undefined && connector.importable;
  // slack/discord interactive-approval cards authorize the tapping user against
  // `authorized_user_ids` — render the inline editor for those (never telegram).
  const showApprovers = connector.is_active && APPROVER_LIST_CONNECTORS.has(connector.connector);

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
        {/* The green "연결됨" pill above is the single connected indicator (no
            duplicate "Connected as @login" chip — connectedLabel stays null).
            A connected oauth-capable binding also surfaces a "Reconnect with X"
            action so the credential can be re-authed on demand — to MIGRATE a
            PAT-backed binding onto OAuth, or to ROTATE/RECOVER an OAuth token —
            without first revoking. Previously this only appeared on the
            backend-driven needs_reauth state, leaving healthy rotation and
            PAT→OAuth migration with no UI path. */}
        {isOAuthConnector(connector.connector) && connector.is_active ? (
          <div className="connector-card__oauth">
            {connector.connector === "github" ? (
              <GithubAppSetup
                configured
                connectedLabel={null}
                needsReauth={connector.needs_reauth}
                connected
              />
            ) : (
              <ConnectorOAuthButton
                provider={connector.connector}
                connectedLabel={null}
                needsReauth={connector.needs_reauth}
                connected
              />
            )}
          </div>
        ) : null}
        {/* The detail line shows the inbound webhook token's last 4 + the
            external ref. An outbound OAuth connector (github, …) has no inbound
            webhook, so the hint is meaningless and the repo ref is redundant on
            a connected card — drop the whole line for oauth connectors. */}
        {isOAuthConnector(connector.connector) ? null : (
          <p className="connector-card__detail">
            {connector.external_ref ? (
              <span className="connector-card__ref">{connector.external_ref}</span>
            ) : null}
            <span className="connector-card__hint" title={t("tokenHintTitle")}>
              {connector.token_hint}
            </span>
          </p>
        )}
        {lastImportAt !== null && lastImportAt !== undefined ? (
          <p className="connector-card__import-stamp" aria-live="polite">
            {t("lastImported", {
              count: lastImportCount ?? 0,
              at: lastImportAt,
            })}
          </p>
        ) : null}
        {showApprovers ? (
          <ApproversEditor
            connector={connector}
            onSaved={onUpdated}
            updateConnector={updateConnector}
          />
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
            {/* slack/discord surface a real approvers editor above, so the
                "coming soon" Configure stub would be misleading — hide it. */}
            {showApprovers ? null : (
              <button
                type="button"
                className="connector-card__ghost"
                disabled
                title={t("configureTitle")}
              >
                {t("configure")}
              </button>
            )}
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
