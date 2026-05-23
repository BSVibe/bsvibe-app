"use client";

import { createConnector, listConnectors, revokeConnector } from "@/lib/api/connectors";
import type { Connector, ConnectorName } from "@/lib/api/types";
import { KNOWN_CONNECTORS } from "@/lib/api/types";
import { useEffect, useRef, useState } from "react";
import AddConnector from "./AddConnector";
import ConnectorRow from "./ConnectorRow";

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

/** Pretty display names for the supported KNOWN_CONNECTORS in the catalog. */
const SUPPORTED_LABELS: Record<ConnectorName, string> = {
  github: "GitHub",
  slack: "Slack",
  telegram: "Telegram",
  discord: "Discord",
  sentry: "Sentry",
  notion: "Notion",
  "email-sender": "Email",
};

/** Short one-liners shown under each supported connector's Connect card. */
const SUPPORTED_BLURBS: Record<ConnectorName, string> = {
  github: "issues, PRs and pushes",
  slack: "messages and channels",
  telegram: "messages and channels",
  discord: "messages and channels",
  sentry: "errors and issues",
  notion: "pages and databases",
  "email-sender": "outbound delivery",
};

/** The design's aspirational services — rendered as disabled coming-soon cards
 *  so the catalog visually matches the design. No backend exists for these. */
const ASPIRATIONAL: { name: string; blurb: string }[] = [
  { name: "Figma", blurb: "design files, reads and comments" },
  { name: "Linear", blurb: "issues and projects" },
  { name: "Google Drive", blurb: "docs, sheets, slides" },
  { name: "PowerPoint", blurb: "create .pptx via Graph" },
  { name: "Postgres", blurb: "SQL data sources" },
];

const COMING_SOON = "Coming soon — there’s no backend for this yet.";

export default function Connectors() {
  const [list, setList] = useState<ListState>(null);
  // The connector the founder is mid-creating, if any → renders the create panel
  // pre-selected. `null` = no panel open.
  const [connecting, setConnecting] = useState<ConnectorName | null>(null);
  const dialogRef = useRef<HTMLDialogElement>(null);

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

  useEffect(() => {
    let active = true;
    listConnectors()
      .then((data) => active && setList({ data, failed: false }))
      .catch(() => active && setList({ data: [], failed: true }));
    return () => {
      active = false;
    };
  }, []);

  const connected = list && !list.failed ? list.data.filter((c) => c.is_active) : [];
  const connectedNames = new Set(connected.map((c) => c.connector));
  // Supported connectors not yet connected → real Connect cards.
  const available = KNOWN_CONNECTORS.filter((name) => !connectedNames.has(name));

  return (
    <section className="connectors" aria-label="Connectors">
      <header className="connectors__head">
        <h2 className="section-label">Connectors</h2>
        {connected.length > 0 ? (
          <span className="connectors__count">{connected.length}</span>
        ) : null}
      </header>
      <p className="connectors__lede">
        The external systems I can read from and act on. Connect a service to give it a private
        webhook to reach me, and an optional place for me to deliver finished work back out.
      </p>

      {/* CONNECTED ───────────────────────────────────────────────────────── */}
      <div className="connectors__section">
        <h3 className="connectors__section-label">Connected</h3>
        {list === null ? (
          <p className="connectors__loading" aria-busy="true">
            Loading your connectors…
          </p>
        ) : list.failed ? (
          <p className="connectors__note" aria-live="polite">
            Couldn&rsquo;t load your connectors right now — try again in a moment.
          </p>
        ) : connected.length === 0 ? (
          <p className="connectors__empty">
            Nothing connected yet. Pick a service below to wire one up.
          </p>
        ) : (
          <ul className="connectors__grid" aria-label="Connected services">
            {connected.map((c) => (
              <ConnectorRow key={c.id} connector={c} onRevoked={load} revoke={revokeConnector} />
            ))}
          </ul>
        )}
      </div>

      {/* AVAILABLE ────────────────────────────────────────────────────────── */}
      <div className="connectors__section">
        <h3 className="connectors__section-label">Available</h3>
        <ul className="connectors__grid" aria-label="Available services">
          {available.map((name) => (
            <li key={name} className="connector-card connector-card--available">
              <div className="connector-card__body">
                <span className="connector-card__name">{SUPPORTED_LABELS[name]}</span>
                <p className="connector-card__detail">{SUPPORTED_BLURBS[name]}</p>
              </div>
              <div className="connector-card__actions">
                <button
                  type="button"
                  className="connector-card__connect"
                  onClick={() => setConnecting(name)}
                >
                  Connect
                </button>
              </div>
            </li>
          ))}
          {ASPIRATIONAL.map((svc) => (
            <li key={svc.name} className="connector-card connector-card--available">
              <div className="connector-card__body">
                <span className="connector-card__name">{svc.name}</span>
                <p className="connector-card__detail">{svc.blurb}</p>
              </div>
              <div className="connector-card__actions">
                <button
                  type="button"
                  className="connector-card__connect"
                  disabled
                  title={COMING_SOON}
                >
                  Connect
                </button>
              </div>
            </li>
          ))}
        </ul>
      </div>

      {/* CUSTOM (MCP) ─────────────────────────────────────────────────────── */}
      <div className="connector-custom">
        <div className="connector-custom__body">
          <p className="connector-custom__title">Add a custom Connector</p>
          <p className="connector-custom__detail">
            Point me at your own MCP server endpoint or a BSage plugin built with the plugin SDK.
          </p>
        </div>
        <button type="button" className="connector-custom__action" disabled title={COMING_SOON}>
          Add custom
        </button>
      </div>

      {/* CREATE PANEL — a native <dialog> hosting the real create +
          one-time-token reveal flow. showModal() gives the backdrop + focus
          trap + Escape-to-close; onClose mirrors a dismiss back into state. */}
      <dialog
        ref={dialogRef}
        className="connector-modal"
        aria-label="Connect a service"
        onClose={() => setConnecting(null)}
      >
        {connecting ? (
          <div className="connector-modal__panel">
            <p className="connector-modal__title">Connect {SUPPORTED_LABELS[connecting]}</p>
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
