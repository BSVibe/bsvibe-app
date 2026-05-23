"use client";

import { createConnector, listConnectors, revokeConnector } from "@/lib/api/connectors";
import type { Connector } from "@/lib/api/types";
import { useEffect, useState } from "react";
import AddConnector from "./AddConnector";
import ConnectorRow from "./ConnectorRow";

/**
 * Settings → Connectors. The founder registers / views / revokes the
 * per-workspace connector bindings that wire an external service in (inbound
 * webhook) and out (outbound delivery target). Backed by the REAL
 * /api/v1/connectors endpoints (backend/api/v1/connectors.py).
 *
 *  - List   ← GET /api/v1/connectors    (masked token_hint, never the secret)
 *  - Add    → POST /api/v1/connectors   (201 returns the one-time webhook_url +
 *             webhook_token — a capability shown ONCE, surfaced by AddConnector)
 *  - Revoke → DELETE /api/v1/connectors/{id} (soft-revoke; confirm-gated)
 *
 * The list loads on mount and re-reads after a successful create or revoke so
 * the section always reflects the server. A failed list read degrades to a calm
 * inline note rather than a blanked page; the Add form still works (its own
 * read is independent of the list's success).
 */
type ListState = { data: Connector[]; failed: boolean } | null;

export default function Connectors() {
  const [list, setList] = useState<ListState>(null);

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

  return (
    <section className="connectors" aria-label="Connectors">
      <header className="connectors__head">
        <h2 className="section-label">Connectors</h2>
        {list && !list.failed && list.data.length > 0 ? (
          <span className="connectors__count">{list.data.length}</span>
        ) : null}
      </header>
      <p className="connectors__lede">
        Each connector gives a service a private webhook to reach me, and an optional place for me
        to deliver finished work back out.
      </p>

      <AddConnector onCreated={load} createConnector={createConnector} />

      {list === null ? (
        <p className="connectors__loading" aria-busy="true">
          Loading your connectors…
        </p>
      ) : list.failed ? (
        <p className="connectors__note" aria-live="polite">
          Couldn&rsquo;t load your connectors right now — try again in a moment.
        </p>
      ) : list.data.length === 0 ? (
        <p className="connectors__empty">No connectors yet. Add one above to wire up a service.</p>
      ) : (
        <ul className="connectors__list">
          {list.data.map((c) => (
            <ConnectorRow key={c.id} connector={c} onRevoked={load} revoke={revokeConnector} />
          ))}
        </ul>
      )}
    </section>
  );
}
