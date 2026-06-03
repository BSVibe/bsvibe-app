/** Connectors API — REAL backend `/api/v1/connectors`
 *  (backend/api/v1/connectors.py): the founder's front door for wiring an
 *  external service to the workspace.
 *
 *   GET    /api/v1/connectors           — list registered connectors (masked
 *                                          `token_hint`, never the secret)
 *   POST   /api/v1/connectors           — register one; the 201 response is the
 *                                          ONLY place the `webhook_token` +
 *                                          full `webhook_url` are returned
 *   DELETE /api/v1/connectors/{id}      — soft-revoke (flips `is_active` False),
 *                                          204 No Content
 *
 *  The create body mirrors the backend `ConnectorCreate` (extra=forbid) 1:1: we
 *  only send fields the schema declares. `delivery_config` defaults to `{}` so
 *  the wire shape is stable for an inbound-only connector; `external_ref` is
 *  omitted entirely when blank rather than sent as an empty string. */

import { apiFetch } from "./client";
import type { Connector, ConnectorCreate, ConnectorCreated, ConnectorImportResult } from "./types";

/** Registered connectors for the active workspace (newest first). */
export function listConnectors(): Promise<Connector[]> {
  return apiFetch<Connector[]>("/api/v1/connectors");
}

/** Register a connector. The 201 response carries the one-time `webhook_token`
 *  + `webhook_url` (a capability — show once, then it's gone). We build the
 *  body to match the backend extra=forbid schema: drop `external_ref` when
 *  empty, always send `delivery_config` (default `{}`). */
export function createConnector(input: ConnectorCreate): Promise<ConnectorCreated> {
  const body: ConnectorCreate = {
    connector: input.connector,
    signing_secret: input.signing_secret,
    delivery_config: input.delivery_config ?? {},
  };
  const externalRef = input.external_ref?.trim();
  if (externalRef) body.external_ref = externalRef;

  return apiFetch<ConnectorCreated>("/api/v1/connectors", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Soft-revoke a connector by id — the backend flips `is_active` False and the
 *  ingress 404s on it thereafter. 204 No Content, so this resolves to void. */
export function revokeConnector(connectorId: string): Promise<void> {
  return apiFetch<void>(`/api/v1/connectors/${encodeURIComponent(connectorId)}`, {
    method: "DELETE",
  });
}

/** Lift B — trigger an inbound bulk import for a connector (Obsidian vault
 *  scan, Claude/GPT conversation export, Notion page walk). The route reads
 *  the binding's `delivery_config` as the import config so the founder
 *  doesn't re-type it. Returns `{ imported_count, last_import_at, detail }`;
 *  the row re-reads the list afterwards so the new "Last imported" stamp
 *  appears.
 *
 *  Failure modes the server returns:
 *   - 404 — id unknown / revoked for this workspace
 *   - 422 — connector is outbound-only (e.g. github) OR push-only inbound
 *           (slack — its inbound is webhook-driven)
 *   - 502 — the plugin import action failed (e.g. vault path missing) */
export function triggerImport(connectorId: string): Promise<ConnectorImportResult> {
  return apiFetch<ConnectorImportResult>(
    `/api/v1/connectors/${encodeURIComponent(connectorId)}/import`,
    {
      method: "POST",
      body: JSON.stringify({}),
    },
  );
}
