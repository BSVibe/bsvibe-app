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
import type {
  Connector,
  ConnectorCatalog,
  ConnectorCreate,
  ConnectorCreated,
  ConnectorImportResult,
} from "./types";

/** Registered connectors for the active workspace (newest first). */
export function listConnectors(): Promise<Connector[]> {
  return apiFetch<Connector[]>("/api/v1/connectors");
}

/** The founder-visible connector catalog (INV-1 single source of truth,
 *  backend derives it from PluginMeta). Drives the create-form picker and the
 *  AVAILABLE cards — only `user_connectable` connectors are returned, so
 *  suppressed ones (linear / trello) are naturally absent. Each entry carries
 *  the capability flags the UI branches on (outbound / importable /
 *  webhook_trigger). */
export function getConnectorCatalog(): Promise<ConnectorCatalog> {
  return apiFetch<ConnectorCatalog>("/api/v1/connectors/catalog");
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

/** Response of `POST /api/v1/connectors/oauth/{provider}/start`. */
export interface ConnectorOAuthStart {
  authorize_url: string;
}

/** Begin the OAuth connect dance for `provider` (github / slack / …). The
 *  backend mints CSRF state + PKCE and returns the provider authorize URL the
 *  browser must navigate to. */
export function startConnectorOAuth(provider: string): Promise<ConnectorOAuthStart> {
  return apiFetch<ConnectorOAuthStart>(
    `/api/v1/connectors/oauth/${encodeURIComponent(provider)}/start`,
    { method: "POST" },
  );
}

/** Operator: configure a vanilla OAuth provider's App credentials (slack/notion/
 *  discord) by pasting the client_id/secret created in that provider's console.
 *  Stored encrypted server-side; the provider registers so workspaces can then
 *  1-click connect. github uses the manifest flow, not this. */
export function setProviderAppCredentials(
  provider: string,
  clientId: string,
  clientSecret: string,
  appSlug?: string,
): Promise<{ provider: string; configured: boolean }> {
  const body: Record<string, string> = { client_id: clientId, client_secret: clientSecret };
  // sentry requires its integration slug (for the external-install URL); other
  // providers ignore it, so only send it when present.
  if (appSlug?.trim()) body.app_slug = appSlug.trim();
  return apiFetch(`/api/v1/connectors/oauth/${encodeURIComponent(provider)}/app-credentials`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Response of `GET /api/v1/connectors/oauth/sentry/install-url`. */
export interface SentryInstallUrl {
  configured: boolean;
  install_url: string | null;
}

/** The Sentry external-install URL the founder opens to install + authorize.
 *  `configured` is false until the operator has set Sentry's creds + slug. */
export function getSentryInstallUrl(): Promise<SentryInstallUrl> {
  return apiFetch<SentryInstallUrl>("/api/v1/connectors/oauth/sentry/install-url");
}

/** An install awaiting a workspace claim (Sentry claim-later). No secrets. */
export interface UnclaimedInstall {
  id: string;
  provider: string;
  installation_ref: string;
  account_label: string | null;
  created_at: string;
}

/** Installs exchanged but not yet bound to a workspace (claim-later). */
export function listUnclaimedInstalls(): Promise<{ unclaimed: UnclaimedInstall[] }> {
  return apiFetch<{ unclaimed: UnclaimedInstall[] }>("/api/v1/connectors/oauth/unclaimed");
}

/** Bind an unclaimed install to the active workspace. */
export function claimInstall(
  unclaimedId: string,
): Promise<{ connector: string; claimed: boolean }> {
  return apiFetch(`/api/v1/connectors/oauth/unclaimed/${encodeURIComponent(unclaimedId)}/claim`, {
    method: "POST",
  });
}

/** Response of `GET /api/v1/connectors/oauth/github/app-status`. */
export interface GithubAppStatus {
  configured: boolean;
  app_slug: string | null;
  html_url: string | null;
}

/** Whether the bsvibe GitHub App is set up (so "Connect with GitHub" works) or
 *  the founder still needs to create it via the manifest flow. */
export function getGithubAppStatus(): Promise<GithubAppStatus> {
  return apiFetch<GithubAppStatus>("/api/v1/connectors/oauth/github/app-status");
}

/** Response of `POST /api/v1/connectors/oauth/github/app-manifest/start`. The
 *  PWA auto-submits `manifest` (JSON) as a form POST to `post_url`; GitHub
 *  creates the App and redirects back to the manifest callback. */
export interface GithubAppManifestStart {
  post_url: string;
  manifest: Record<string, unknown>;
}

/** Begin the GitHub App Manifest flow — returns the GitHub POST target + the
 *  manifest body to submit. */
export function startGithubAppManifest(): Promise<GithubAppManifestStart> {
  return apiFetch<GithubAppManifestStart>("/api/v1/connectors/oauth/github/app-manifest/start", {
    method: "POST",
  });
}
