"use client";

/**
 * ApproversEditor — the founder-facing editor for a slack/discord connector's
 * INTERACTIVE-APPROVAL allowlist. Slack + Discord render Approve/Reject
 * deliverable cards; the tapping user is authorized against the connector's
 * `delivery_config.authorized_user_ids` (a fail-closed allowlist), optionally
 * scoped to a `team_id` (slack) / `guild_id` (discord).
 *
 * This inline form lets the founder SET that allowlist from Settings — the only
 * way to edit a connector's `delivery_config` after creation. It sends a PARTIAL
 * `delivery_config` (only the keys it edits); the backend shallow-merges, so the
 * inbound `webhook_secret` and any routing keys the founder never sees are
 * preserved server-side. It is rendered ONLY for slack/discord (telegram
 * authorizes via `chat_id`, not a user list — see ConnectorRow).
 *
 * `updateConnector` is injected (default to the real client) for unit testability.
 */

import { updateConnector as realUpdateConnector } from "@/lib/api/connectors";
import type { Connector } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useState } from "react";

type SaveState = "idle" | "saving" | "saved" | "error";

/** Split a textarea value into a de-duplicated, trimmed list of user ids
 *  (one per line OR comma-separated — the founder can paste either). */
function parseUserIds(raw: string): string[] {
  const seen = new Set<string>();
  const ids: string[] = [];
  for (const token of raw.split(/[\n,]/)) {
    const id = token.trim();
    if (id.length > 0 && !seen.has(id)) {
      seen.add(id);
      ids.push(id);
    }
  }
  return ids;
}

function initialIds(connector: Connector): string {
  const value = connector.delivery_config?.authorized_user_ids;
  return Array.isArray(value) ? value.map(String).join("\n") : "";
}

function initialScope(connector: Connector, key: string): string {
  const value = connector.delivery_config?.[key];
  return value === undefined || value === null ? "" : String(value);
}

export function ApproversEditor({
  connector,
  onSaved,
  updateConnector = realUpdateConnector,
}: {
  connector: Connector;
  /** Re-read hook so the parent list reflects the saved config (optional). */
  onSaved?: () => void;
  updateConnector?: (
    id: string,
    patch: { delivery_config: Record<string, unknown> },
  ) => Promise<Connector>;
}) {
  const t = useTranslations("settings.connectors.approvers");
  // Discord scopes by guild, Slack by team — the same optional "which
  // workspace" narrowing under a different provider key.
  const scopeKey = connector.connector === "discord" ? "guild_id" : "team_id";
  const scopeLabelKey = connector.connector === "discord" ? "guildLabel" : "teamLabel";

  const [ids, setIds] = useState<string>(() => initialIds(connector));
  const [scope, setScope] = useState<string>(() => initialScope(connector, scopeKey));
  const [state, setState] = useState<SaveState>("idle");

  const idsFieldId = `approvers-ids-${connector.id}`;
  const scopeFieldId = `approvers-scope-${connector.id}`;

  async function save() {
    if (state === "saving") return;
    setState("saving");
    // Send ONLY the keys this editor owns — the backend merges, so we never
    // touch (or need to see) the inbound webhook_secret or routing config.
    const deliveryConfig: Record<string, unknown> = {
      authorized_user_ids: parseUserIds(ids),
    };
    const scopeValue = scope.trim();
    if (scopeValue.length > 0) deliveryConfig[scopeKey] = scopeValue;
    try {
      await updateConnector(connector.id, { delivery_config: deliveryConfig });
      setState("saved");
      onSaved?.();
    } catch {
      setState("error");
    }
  }

  return (
    <div className="connector-card__approvers">
      <label className="connector-card__approvers-label" htmlFor={idsFieldId}>
        {t("heading")}
      </label>
      <p className="connector-card__approvers-hint" id={`${idsFieldId}-hint`}>
        {t("hint")}
      </p>
      <textarea
        id={idsFieldId}
        className="connector-card__approvers-ids"
        aria-describedby={`${idsFieldId}-hint`}
        rows={3}
        placeholder={t("placeholder")}
        value={ids}
        onChange={(e) => {
          setIds(e.target.value);
          if (state !== "idle") setState("idle");
        }}
      />
      <label className="connector-card__approvers-scope-label" htmlFor={scopeFieldId}>
        {t(scopeLabelKey)}
      </label>
      <input
        id={scopeFieldId}
        className="connector-card__approvers-scope"
        type="text"
        placeholder={t("scopePlaceholder")}
        value={scope}
        onChange={(e) => {
          setScope(e.target.value);
          if (state !== "idle") setState("idle");
        }}
      />
      <div className="connector-card__approvers-actions">
        <button
          type="button"
          className="connector-card__approvers-save"
          onClick={save}
          disabled={state === "saving"}
        >
          {state === "saving" ? t("saving") : t("save")}
        </button>
        {state === "saved" && (
          <span className="connector-card__approvers-ok" aria-live="polite">
            {t("saved")}
          </span>
        )}
        {state === "error" && (
          <span className="connector-card__approvers-error" role="alert">
            {t("error")}
          </span>
        )}
      </div>
    </div>
  );
}
