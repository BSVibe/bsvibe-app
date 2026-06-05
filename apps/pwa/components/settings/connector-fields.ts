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

const NOTION: ConnectorFormDescriptor = {
  fields: [
    { key: "secret", i18nKey: "signingSecret", kind: "password", required: true },
    // Inbound block — both fields optional; supplying them turns the
    // binding into "both" by giving the import action what it needs to
    // walk the Notion workspace.
    {
      key: "api_token",
      i18nKey: "notionApiToken",
      kind: "password",
      required: false,
      group: "inbound",
    },
    {
      key: "database_ids",
      i18nKey: "notionDatabaseIds",
      kind: "textarea",
      required: false,
      group: "inbound",
    },
  ],
  // Notion still surfaces the JSON delivery_config for the outbound side
  // (parent_page_id). The inbound fields above are packed alongside it.
  showDeliveryConfigJson: true,
  pack: (values, connector, externalRef) => {
    const config: Record<string, unknown> = values.deliveryConfigParsed
      ? (JSON.parse(values.deliveryConfigParsed) as Record<string, unknown>)
      : {};
    const apiToken = (values.api_token ?? "").trim();
    if (apiToken.length > 0) config.api_token = apiToken;
    const databaseIds = (values.database_ids ?? "")
      .split("\n")
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
    if (databaseIds.length > 0) config.database_ids = databaseIds;
    return {
      connector,
      signing_secret: values.secret ?? "",
      external_ref: externalRef,
      delivery_config: config,
    };
  },
};

const DESCRIPTORS: Record<ConnectorName, ConnectorFormDescriptor> = {
  github: OUTBOUND_DEFAULT,
  slack: OUTBOUND_DEFAULT,
  telegram: OUTBOUND_DEFAULT,
  discord: OUTBOUND_DEFAULT,
  sentry: OUTBOUND_DEFAULT,
  "email-sender": OUTBOUND_DEFAULT,
  obsidian: OBSIDIAN,
  claude: CONVERSATION_EXPORT(true),
  gpt: CONVERSATION_EXPORT(true),
  notion: NOTION,
};

export function descriptorFor(connector: ConnectorName): ConnectorFormDescriptor {
  return DESCRIPTORS[connector];
}

export { INBOUND_SECRET_PLACEHOLDER };
