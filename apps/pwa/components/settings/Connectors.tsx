"use client";

import {
  type UnclaimedInstall,
  claimInstall,
  createConnector,
  getSentryInstallUrl,
  listConnectors,
  listUnclaimedInstalls,
  revokeConnector,
  startConnectorOAuth,
  triggerImport,
} from "@/lib/api/connectors";
import type { Connector, ConnectorName } from "@/lib/api/types";
import { KNOWN_CONNECTORS } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import AddConnector from "./AddConnector";
import ConnectorRow from "./ConnectorRow";
import { ProviderAppConfig } from "./ProviderAppConfig";
import { isInstallConnector, isOAuthConnector, isPasteCredsConnector } from "./connector-fields";

/**
 * Settings → Connectors, framed as a CATALOG (design: stitch/
 * settings-connectors-v2.1.png). Three sections:
 *
 *  - CONNECTED  — REAL: a card per active connector from GET /api/v1/connectors
 *                 (filtered `is_active`). Each card carries a real, confirm-gated
 *                 Revoke (DELETE) and a present-but-disabled Configure (there is
 *                 no backend update endpoint yet → coming-soon).
 *  - AVAILABLE  — the catalog. The supported KNOWN_CONNECTORS not yet connected
 *                 render as REAL "Connect" cards (Connect opens the create panel
 *                 pre-selected to that service). The design's aspirational
 *                 services (Figma / Linear / Google Drive / PowerPoint /
 *                 Postgres) render as DISABLED coming-soon cards so the catalog
 *                 matches the design while staying honest — there is no backend
 *                 for them.
 *  - CUSTOM     — "Add a custom Connector" (point at your own MCP server / BSage
 *                 plugin SDK). Present per the design but DISABLED — there is no
 *                 custom-MCP backend yet.
 *
 * The list loads on mount and re-reads after a successful create or revoke so
 * the catalog always reflects the server. A failed list read degrades to a calm
 * inline note rather than a blanked page.
 *
 * Backend reality (do NOT change): only list / create / revoke exist. Everything
 * disabled here is a deliberate, honest stub with a `title` hint, not a dead
 * control pretending to work.
 */
type ListState = { data: Connector[]; failed: boolean } | null;

/** The design's aspirational services — rendered as disabled coming-soon cards
 *  so the catalog visually matches the design. No backend exists for these.
 *  Names + blurbs come from the `settings.connectors.aspirational` catalog. */
const ASPIRATIONAL_KEYS = ["figma", "linear", "googleDrive", "powerpoint", "postgres"] as const;

export default function Connectors() {
  const [list, setList] = useState<ListState>(null);
  const t = useTranslations("settings.connectors");
  // The connector the founder is mid-creating, if any → renders the create panel
  // pre-selected. `null` = no panel open.
  const [connecting, setConnecting] = useState<ConnectorName | null>(null);
  // Connector whose OAuth start failed (app not configured by the operator yet)
  // → a calm "not available" note instead of a broken redirect.
  const [oauthUnavailable, setOauthUnavailable] = useState<ConnectorName | null>(null);
  // Provider whose operator paste-creds form is open (slack/notion/discord/
  // sentry not configured yet) → renders ProviderAppConfig inline on its card.
  const [configuring, setConfiguring] = useState<ConnectorName | null>(null);
  // Installs exchanged but not yet bound to a workspace (Sentry claim-later).
  const [unclaimed, setUnclaimed] = useState<UnclaimedInstall[]>([]);
  // Unclaimed id currently being claimed (button busy state).
  const [claiming, setClaiming] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDialogElement>(null);

  // "Connect" on a card. OAuth connectors (github/slack/notion/discord) need no
  // form — the App is operator-configured once (SaaS single-app), so go STRAIGHT
  // to the provider authorize URL, no modal. Everything else opens the create
  // panel for its binding fields.
  async function handleConnect(name: ConnectorName) {
    // Sentry's install→grant flow (claim-later): no /start dance — fetch the
    // external-install URL and navigate there. Unconfigured → operator form.
    if (isInstallConnector(name)) {
      setOauthUnavailable(null);
      try {
        const { configured, install_url } = await getSentryInstallUrl();
        if (configured && install_url) {
          window.location.assign(install_url);
          return;
        }
        setConfiguring(name);
      } catch {
        setConfiguring(name);
      }
      return;
    }
    if (!isOAuthConnector(name)) {
      setConnecting(name);
      return;
    }
    setOauthUnavailable(null);
    try {
      const { authorize_url } = await startConnectorOAuth(name);
      window.location.assign(authorize_url);
    } catch {
      // Provider not configured. Paste-creds providers (slack/notion/discord)
      // → open the operator config form; github → calm note (manifest flow).
      if (isPasteCredsConnector(name)) {
        setConfiguring(name);
      } else {
        setOauthUnavailable(name);
      }
    }
  }

  // After the operator saves a provider's App creds, proceed to the connect.
  async function connectAfterConfig(name: ConnectorName) {
    setConfiguring(null);
    if (isInstallConnector(name)) {
      const { configured, install_url } = await getSentryInstallUrl();
      if (configured && install_url) window.location.assign(install_url);
      return;
    }
    const { authorize_url } = await startConnectorOAuth(name);
    window.location.assign(authorize_url);
  }

  // Claim an unclaimed install to the active workspace, then refresh both lists.
  async function handleClaim(id: string) {
    setClaiming(id);
    try {
      await claimInstall(id);
      await Promise.all([load(), loadUnclaimed()]);
    } catch {
      // leave the row in place; the founder can retry.
    } finally {
      setClaiming(null);
    }
  }

  // Drive the native <dialog> from the `connecting` state: showModal() gives us
  // the backdrop, focus trap, and Escape-to-close for free. close() fires on
  // Escape / backdrop, so we mirror that back into state via onClose. The
  // try/catch keeps this safe in environments that don't implement the dialog
  // methods (e.g. jsdom under test, where showModal() throws "Not implemented")
  // — the panel content is still in the DOM, so the create flow stays
  // exercisable without browser modality.
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    try {
      if (connecting && !dialog.open) dialog.showModal();
      if (!connecting && dialog.open) dialog.close();
    } catch {
      // Modality unavailable in this environment (e.g. jsdom's showModal throws
      // "Not implemented"). Fall back to the plain `open` attribute so the panel
      // content stays in the accessibility tree and the create flow remains
      // exercisable; real browsers take the showModal() path above.
      dialog.open = Boolean(connecting);
    }
  }, [connecting]);

  async function load() {
    try {
      setList({ data: await listConnectors(), failed: false });
    } catch {
      setList({ data: [], failed: true });
    }
  }

  async function loadUnclaimed() {
    try {
      const res = await listUnclaimedInstalls();
      setUnclaimed(res?.unclaimed ?? []);
    } catch {
      // Best-effort — a failed read just hides the pending-installs section.
      setUnclaimed([]);
    }
  }

  useEffect(() => {
    let active = true;
    // Load sequentially (list, then pending installs) so the request order is
    // deterministic and the pending-installs section settles after the catalog.
    (async () => {
      try {
        const data = await listConnectors();
        if (active) setList({ data, failed: false });
      } catch {
        if (active) setList({ data: [], failed: true });
      }
      try {
        const res = await listUnclaimedInstalls();
        if (active) setUnclaimed(res?.unclaimed ?? []);
      } catch {
        // best-effort
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  const connected = list && !list.failed ? list.data.filter((c) => c.is_active) : [];
  const connectedNames = new Set(connected.map((c) => c.connector));
  // Supported connectors not yet connected → real Connect cards.
  const available = KNOWN_CONNECTORS.filter((name) => !connectedNames.has(name));

  return (
    <section className="connectors" aria-label={t("sectionLabel")}>
      <header className="connectors__head">
        <h2 className="section-label">{t("sectionHeading")}</h2>
        {connected.length > 0 ? (
          <span className="connectors__count">{connected.length}</span>
        ) : null}
      </header>
      <p className="connectors__lede">{t("lede")}</p>

      {/* CONNECTED ───────────────────────────────────────────────────────── */}
      <div className="connectors__section">
        <h3 className="connectors__section-label">{t("connected")}</h3>
        {list === null ? (
          <p className="connectors__loading" aria-busy="true">
            {t("loading")}
          </p>
        ) : list.failed ? (
          <p className="connectors__note" aria-live="polite">
            {t("loadError")}
          </p>
        ) : connected.length === 0 ? (
          <p className="connectors__empty">{t("empty")}</p>
        ) : (
          <ul className="connectors__grid" aria-label={t("connectedServices")}>
            {connected.map((c) => (
              <ConnectorRow
                key={c.id}
                connector={c}
                onRevoked={load}
                onImported={load}
                revoke={revokeConnector}
                triggerImport={triggerImport}
              />
            ))}
          </ul>
        )}
      </div>

      {/* PENDING INSTALLS (claim-later) ───────────────────────────────────── */}
      {unclaimed.length > 0 ? (
        <div className="connectors__section">
          <h3 className="connectors__section-label">{t("unclaimed.heading")}</h3>
          <p className="connectors__lede">{t("unclaimed.lede")}</p>
          <ul className="connectors__grid" aria-label={t("unclaimed.heading")}>
            {unclaimed.map((u) => (
              <li key={u.id} className="connector-card connector-card--available">
                <div className="connector-card__body">
                  <span className="connector-card__name">
                    {t(`labels.${u.provider as ConnectorName}`)}
                  </span>
                  <p className="connector-card__detail">{u.account_label ?? u.installation_ref}</p>
                </div>
                <div className="connector-card__actions">
                  <button
                    type="button"
                    className="connector-card__connect"
                    disabled={claiming === u.id}
                    onClick={() => handleClaim(u.id)}
                  >
                    {claiming === u.id ? t("unclaimed.claiming") : t("unclaimed.claim")}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* AVAILABLE ────────────────────────────────────────────────────────── */}
      <div className="connectors__section">
        <h3 className="connectors__section-label">{t("available")}</h3>
        <ul className="connectors__grid" aria-label={t("availableServices")}>
          {available.map((name) => (
            <li key={name} className="connector-card connector-card--available">
              <div className="connector-card__body">
                <span className="connector-card__name">{t(`labels.${name}`)}</span>
                <p className="connector-card__detail">{t(`blurbs.${name}`)}</p>
              </div>
              {configuring === name ? (
                <ProviderAppConfig
                  provider={name}
                  requireSlug={isInstallConnector(name)}
                  onSaved={() => connectAfterConfig(name)}
                  onCancel={() => setConfiguring(null)}
                />
              ) : (
                <div className="connector-card__actions">
                  {oauthUnavailable === name ? (
                    <span className="connector-card__note" aria-live="polite">
                      {t("oauthUnavailable")}
                    </span>
                  ) : null}
                  <button
                    type="button"
                    className="connector-card__connect"
                    onClick={() => handleConnect(name)}
                  >
                    {t("connect")}
                  </button>
                </div>
              )}
            </li>
          ))}
          {ASPIRATIONAL_KEYS.map((key) => (
            <li key={key} className="connector-card connector-card--available">
              <div className="connector-card__body">
                <span className="connector-card__name">{t(`aspirational.${key}.name`)}</span>
                <p className="connector-card__detail">{t(`aspirational.${key}.blurb`)}</p>
              </div>
              <div className="connector-card__actions">
                <button
                  type="button"
                  className="connector-card__connect"
                  disabled
                  title={t("comingSoon")}
                >
                  {t("connect")}
                </button>
              </div>
            </li>
          ))}
        </ul>
      </div>

      {/* CUSTOM (MCP) ─────────────────────────────────────────────────────── */}
      <div className="connector-custom">
        <div className="connector-custom__body">
          <p className="connector-custom__title">{t("customTitle")}</p>
          <p className="connector-custom__detail">{t("customDetail")}</p>
        </div>
        <button type="button" className="connector-custom__action" disabled title={t("comingSoon")}>
          {t("addCustom")}
        </button>
      </div>

      {/* CREATE PANEL — a native <dialog> hosting the real create +
          one-time-token reveal flow. showModal() gives the backdrop + focus
          trap + Escape-to-close; onClose mirrors a dismiss back into state. */}
      <dialog
        ref={dialogRef}
        className="connector-modal"
        aria-label={t("connectDialogLabel")}
        onClose={() => setConnecting(null)}
      >
        {connecting ? (
          <div className="connector-modal__panel">
            <p className="connector-modal__title">
              {t("connectTitle", { service: t(`labels.${connecting}`) })}
            </p>
            <AddConnector
              initialConnector={connecting}
              onCancel={() => setConnecting(null)}
              onCreated={load}
              createConnector={createConnector}
            />
          </div>
        ) : null}
      </dialog>
    </section>
  );
}
