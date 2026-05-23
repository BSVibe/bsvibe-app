"use client";

import type { Connector } from "@/lib/api/types";
import { useState } from "react";

type RowState = "idle" | "confirming" | "revoking" | "error";

/**
 * One registered connector. Shows the connector name, its reference label (if
 * any), the masked token hint (last 4 — never the full capability), the active
 * state, and any delivery_config keys (the outbound routing the founder set).
 *
 * Revoke is confirm-gated: the first "Revoke" click reveals a "Confirm revoke"
 * (+ "Cancel") so a soft-revoke is never a single stray tap. Confirm fires the
 * DELETE; on success `onRevoked` re-reads the list so the row reflects the new
 * (inactive) state. A failed revoke shows a calm inline note and keeps the row
 * actionable — it never crashes the surface.
 *
 * `revoke` is injected (defaults to the real client) for unit testability.
 */
export default function ConnectorRow({
  connector,
  onRevoked,
  revoke,
}: {
  connector: Connector;
  onRevoked: () => void;
  revoke: (id: string) => Promise<void>;
}) {
  const [state, setState] = useState<RowState>("idle");

  async function confirmRevoke() {
    if (state === "revoking") return;
    setState("revoking");
    try {
      await revoke(connector.id);
      onRevoked();
      // The container re-read will replace this row; leave it in revoking until
      // then so the button can't be re-fired.
    } catch {
      setState("error");
    }
  }

  const configKeys = Object.keys(connector.delivery_config ?? {});

  return (
    <li className="connector-row">
      <div className="connector-row__main">
        <span className="connector-row__name">{connector.connector}</span>
        {connector.external_ref ? (
          <span className="connector-row__ref">{connector.external_ref}</span>
        ) : null}
        <span className="connector-row__hint" title="Webhook token (masked)">
          {connector.token_hint}
        </span>
        <span
          className={`connector-row__state connector-row__state--${
            connector.is_active ? "active" : "revoked"
          }`}
        >
          {connector.is_active ? "Active" : "Revoked"}
        </span>
      </div>

      {configKeys.length > 0 ? (
        <p className="connector-row__config">Delivers out · {configKeys.join(", ")}</p>
      ) : null}

      <div className="connector-row__actions">
        {state === "error" && (
          <span className="connector-row__error" aria-live="polite">
            Couldn&rsquo;t revoke that — please try again.
          </span>
        )}

        {state === "confirming" || state === "revoking" ? (
          <>
            <button
              type="button"
              className="connector-row__danger"
              onClick={confirmRevoke}
              disabled={state === "revoking"}
            >
              {state === "revoking" ? "Revoking…" : "Confirm revoke"}
            </button>
            <button
              type="button"
              className="connector-row__cancel"
              onClick={() => setState("idle")}
              disabled={state === "revoking"}
            >
              Cancel
            </button>
          </>
        ) : connector.is_active ? (
          <button
            type="button"
            className="connector-row__revoke"
            onClick={() => setState("confirming")}
          >
            Revoke
          </button>
        ) : null}
      </div>
    </li>
  );
}
