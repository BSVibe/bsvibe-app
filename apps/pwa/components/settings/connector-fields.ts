/**
 * Per-connector field configuration for the Add Connector form (Lift B).
 *
 * Each connector exposes its own set of binding fields:
 *
 *  - Outbound-only connectors (github / slack / telegram / discord / sentry /
 *    email-sender) — `signing_secret` (the webhook signing secret the public
 *    ingress verifies) + an optional JSON `delivery_config` (founder-set
 *    outbound routing, e.g. notion `{"parent_page_id":"…"}`).
 *
 *  - Inbound-only connectors (Lift B):
 *      • obsidian — `vault_path` + optional `exclude_patterns` (one glob
 *        per line) + optional `default_region`. No webhook secret —
 *        the import is a local-vault scan, but the backend still
 *        requires a non-empty `signing_secret` (it encrypts it). We
 *        send a placeholder string so the wire shape stays uniform.
 *      • claude / gpt — `export_path` (absolute path to
 *        `conversations.json`). Same secret-placeholder note as above.
 *
 *  - Both inbound + outbound (notion) — webhook `signing_secret` + optional
 *    outbound `delivery_config` JSON (`parent_page_id`) + optional inbound
 *    block (`api_token` + `database_ids`). The inbound block is packed into
 *    the same `delivery_config` JSON server-side so the wire shape stays
 *    uniform.
 *
 *  Slack is kind="both" but its inbound is webhook-driven (no bulk-import
 *  action) — so the form treats it like an outbound connector for binding
 *  purposes; no inbound fields show.
 *
 * The form reads the descriptor for the active connector and renders the
 * declared inputs in order. `pack(values)` builds the `ConnectorCreate`
 * payload — including the placeholder `signing_secret` for connectors
 * that don't have one.
 */

import type { ConnectorCreate, ConnectorName } from "@/lib/api/types";

export type FieldKind = "text" | "password" | "textarea" | "oauth";

export interface FieldDescriptor {
  /** State key inside the form. */
  key: string;
  /** Translation key tail under `settings.connectors.form.fields.<key>` —
   *  resolved to `label` / `placeholder` / `hint` at render time. */
  i18nKey: string;
  kind: FieldKind;
  required: boolean;
  /** When set, the field renders inside the optional "inbound config"
   *  section (notion's both-mode `api_token` + `database_ids`) so the
   *  layout can group them under a sub-heading. */
  group?: "inbound" | "outbound";
  /** For `kind: "oauth"` — the provider to connect (e.g. "github"). The form
   *  renders a "Connect with X" button instead of an input. No descriptor
   *  uses this yet; Lift 1 flips github's descriptor over to it. */
  oauthProvider?: string;
}

export interface ConnectorFormDescriptor {
  /** Which fields the form renders for this connector, in order. */
  fields: readonly FieldDescriptor[];
  /** Whether the form should render the legacy JSON `delivery_config`
   *  textarea (true for the existing outbound connectors). When false,
   *  per-connector inbound fields above replace it. */
  showDeliveryConfigJson: boolean;
  /** Build the wire payload from the collected values. Returns the
   *  ready-to-send `ConnectorCreate` body (signing_secret + delivery_config). */
  pack: (
    values: Record<string, string>,
    connector: ConnectorName,
    externalRef: string,
  ) => ConnectorCreate;
}

/** Backend requires `signing_secret` non-empty (it encrypts it at rest).
 *  Inbound-only connectors have no real signing secret — they're local /
 *  pull-based — so we send a stable, non-secret placeholder. */
const INBOUND_SECRET_PLACEHOLDER = "no-webhook-secret";

const OUTBOUND_DEFAULT: ConnectorFormDescriptor = {
  fields: [{ key: "secret", i18nKey: "signingSecret", kind: "password", required: true }],
  showDeliveryConfigJson: true,
  pack: (values, connector, externalRef) => ({
    connector,
    signing_secret: values.secret ?? "",
    external_ref: externalRef,
    // The JSON textarea is parsed separately in the form (so we can show
    // a JSON-parse error inline); the form passes the parsed dict in via
    // `values.deliveryConfig` as a JSON string of the already-validated
    // object. Empty string → `{}`.
    delivery_config: values.deliveryConfigParsed
      ? (JSON.parse(values.deliveryConfigParsed) as Record<string, unknown>)
      : {},
  }),
};

const OBSIDIAN: ConnectorFormDescriptor = {
  fields: [
    { key: "vault_path", i18nKey: "vaultPath", kind: "text", required: true, group: "inbound" },
    {
      key: "exclude_patterns",
      i18nKey: "excludePatterns",
      kind: "textarea",
      required: false,
      group: "inbound",
    },
    {
      key: "default_region",
      i18nKey: "defaultRegion",
      kind: "text",
      required: false,
      group: "inbound",
    },
  ],
  showDeliveryConfigJson: false,
  pack: (values, connector, externalRef) => {
    const config: Record<string, unknown> = { vault_path: values.vault_path?.trim() ?? "" };
    const excludes = (values.exclude_patterns ?? "")
      .split("\n")
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
    if (excludes.length > 0) config.exclude_patterns = excludes;
    const region = (values.default_region ?? "").trim();
    if (region.length > 0) config.default_region = region;
    return {
      connector,
      signing_secret: INBOUND_SECRET_PLACEHOLDER,
      external_ref: externalRef,
      delivery_config: config,
    };
  },
};

const CONVERSATION_EXPORT = (defaultRegionField: boolean): ConnectorFormDescriptor => ({
  fields: [
    { key: "export_path", i18nKey: "exportPath", kind: "text", required: true, group: "inbound" },
    ...(defaultRegionField
      ? [
          {
            key: "default_region",
            i18nKey: "defaultRegion",
            kind: "text" as const,
            required: false,
            group: "inbound" as const,
          },
        ]
      : []),
  ],
  showDeliveryConfigJson: false,
  pack: (values, connector, externalRef) => {
    const config: Record<string, unknown> = { export_path: values.export_path?.trim() ?? "" };
    const region = (values.default_region ?? "").trim();
    if (region.length > 0) config.default_region = region;
    return {
      connector,
      signing_secret: INBOUND_SECRET_PLACEHOLDER,
      external_ref: externalRef,
      delivery_config: config,
    };
  },
});

/** OAuth-method connectors (design §3.1 Bucket A). The credential is acquired
 *  via "Connect with X" (the backend ``/oauth/{provider}/start`` dance), not a
 *  pasted secret — so the form renders the Connect button instead of a password
 *  field. Lift 1 ships github; slack / discord / notion / sentry cascade. */
const OAUTH_CONNECTORS = new Set<ConnectorName>(["github", "slack", "discord", "notion"]);

/** True when ``connector``'s primary credential is acquired via OAuth (so its
 *  card shows Connect / "Connected as …" instead of a masked secret hint). */
export function isOAuthConnector(connector: ConnectorName): boolean {
  return OAUTH_CONNECTORS.has(connector);
}

/** OAuth providers the operator configures by PASTING client_id/secret (they
 *  have no GitHub-App-style manifest auto-create). github is excluded — it uses
 *  the manifest flow. Mirrors backend ``bootstrap.VANILLA_DB_PROVIDERS``. */
const PASTE_CREDS_CONNECTORS = new Set<ConnectorName>(["slack", "discord", "notion"]);

/** True when the operator sets this provider's App up by pasting client_id/
 *  secret (vs github's manifest). */
export function isPasteCredsConnector(connector: ConnectorName): boolean {
  return PASTE_CREDS_CONNECTORS.has(connector);
}

/** Providers connected via an install→grant flow with no per-connect state, so
 *  the binding is deferred (claim-later): the founder opens the install URL, the
 *  callback parks an unclaimed install, then they claim it to a workspace.
 *  Sentry is the first (design §11); operator setup needs an integration slug. */
const INSTALL_CONNECTORS = new Set<ConnectorName>(["sentry"]);

/** True when ``connector`` uses the install→grant claim-later flow (sentry),
 *  not the standard OAuth start/callback. */
export function isInstallConnector(connector: ConnectorName): boolean {
  return INSTALL_CONNECTORS.has(connector);
}

/** github (Lift 1): "Connect with GitHub" replaces the old PAT/signing-secret
 *  field; the OAuth token is the outbound API credential (stored separately in
 *  connector_oauth_tokens). The delivery_config JSON stays for PR routing
 *  (``{"repo":"owner/name"}``). The backend signing_secret column is NOT NULL,
 *  so pack sends a non-secret placeholder — the real outbound credential is the
 *  OAuth token, attached by the callback to this binding. */
const GITHUB_OAUTH: ConnectorFormDescriptor = {
  fields: [
    {
      key: "github",
      i18nKey: "githubConnect",
      kind: "oauth",
      required: false,
      oauthProvider: "github",
    },
  ],
  showDeliveryConfigJson: true,
  pack: (values, connector, externalRef) => ({
    connector,
    signing_secret: INBOUND_SECRET_PLACEHOLDER,
    external_ref: externalRef,
    delivery_config: values.deliveryConfigParsed
      ? (JSON.parse(values.deliveryConfigParsed) as Record<string, unknown>)
      : {},
  }),
};

/** Generic "Connect with X" descriptor (Lift 2-4) for vanilla-OAuth connectors
 *  whose only binding config is the optional outbound delivery_config JSON
 *  (slack channel routing, discord webhook target). Like GITHUB_OAUTH but with
 *  the shared `oauthConnect` label and no provider-specific extras. */
function oauthSimple(provider: ConnectorName): ConnectorFormDescriptor {
  return {
    fields: [
      {
        key: provider,
        i18nKey: "oauthConnect",
        kind: "oauth",
        required: false,
        oauthProvider: provider,
      },
    ],
    showDeliveryConfigJson: true,
    pack: (values, connector, externalRef) => ({
      connector,
      signing_secret: INBOUND_SECRET_PLACEHOLDER,
      external_ref: externalRef,
      delivery_config: values.deliveryConfigParsed
        ? (JSON.parse(values.deliveryConfigParsed) as Record<string, unknown>)
        : {},
    }),
  };
}

/** notion (Lift 3): "Connect with Notion" replaces the api_token/secret fields
 *  (OAuth provides the token); the optional inbound `database_ids` block + the
 *  outbound delivery_config (parent_page_id) stay. */
const NOTION_OAUTH: ConnectorFormDescriptor = {
  fields: [
    {
      key: "notion",
      i18nKey: "oauthConnect",
      kind: "oauth",
      required: false,
      oauthProvider: "notion",
    },
    {
      key: "database_ids",
      i18nKey: "notionDatabaseIds",
      kind: "textarea",
      required: false,
      group: "inbound",
    },
  ],
  showDeliveryConfigJson: true,
  pack: (values, connector, externalRef) => {
    const config: Record<string, unknown> = values.deliveryConfigParsed
      ? (JSON.parse(values.deliveryConfigParsed) as Record<string, unknown>)
      : {};
    const databaseIds = (values.database_ids ?? "")
      .split("\n")
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
    if (databaseIds.length > 0) config.database_ids = databaseIds;
    return {
      connector,
      signing_secret: INBOUND_SECRET_PLACEHOLDER,
      external_ref: externalRef,
      delivery_config: config,
    };
  },
};

const DESCRIPTORS: Record<ConnectorName, ConnectorFormDescriptor> = {
  github: GITHUB_OAUTH,
  slack: oauthSimple("slack"),
  telegram: OUTBOUND_DEFAULT,
  discord: oauthSimple("discord"),
  sentry: OUTBOUND_DEFAULT,
  "email-sender": OUTBOUND_DEFAULT,
  obsidian: OBSIDIAN,
  claude: CONVERSATION_EXPORT(true),
  gpt: CONVERSATION_EXPORT(true),
  notion: NOTION_OAUTH,
};

export function descriptorFor(connector: ConnectorName): ConnectorFormDescriptor {
  return DESCRIPTORS[connector];
}

export { INBOUND_SECRET_PLACEHOLDER };
