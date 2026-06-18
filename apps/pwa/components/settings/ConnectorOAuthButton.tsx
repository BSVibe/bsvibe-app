"use client";

/**
 * ConnectorOAuthButton — the "Connect with X" control for oauth-method
 * connectors (Lift 0 skeleton; design §7). Not-connected → a button that
 * starts the OAuth dance (POST .../start) and navigates the browser to the
 * returned authorize URL. Connected → the connected identity, no button.
 *
 * `onRedirect` is injectable so the redirect is testable without touching
 * `window.location`; it defaults to a real navigation in the browser.
 */

import { startConnectorOAuth } from "@/lib/api/connectors";
import { useState } from "react";

function titleCase(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function ConnectorOAuthButton({
  provider,
  connectedLabel,
  needsReauth,
  onRedirect,
}: {
  provider: string;
  /** When set, the connector is already connected — show the identity. */
  connectedLabel?: string | null;
  /** Lift E46 — render a `Reconnect with X` CTA instead of the steady
   *  `Connected as` chip when the bound OAuth token row was flipped to
   *  `needs_reauth` (the backend caught a `bad_refresh_token` on the
   *  last refresh attempt). Clicking re-enters the same OAuth flow as
   *  a fresh connect; on success the row's status flips back to
   *  `active` on the first dispatch. */
  needsReauth?: boolean;
  /** Override the post-start navigation (tests). */
  onRedirect?: (url: string) => void;
}) {
  const [busy, setBusy] = useState(false);

  // E46 — when the binding needs re-auth, hide the steady-state chip and
  // fall through to the button branch so the user has a one-click recovery.
  if (connectedLabel && !needsReauth) {
    return (
      <div className="connector-form__oauth-connected" data-provider={provider}>
        Connected as {connectedLabel} ✓
      </div>
    );
  }

  const handleClick = async () => {
    setBusy(true);
    try {
      const { authorize_url } = await startConnectorOAuth(provider);
      const go =
        onRedirect ??
        ((url: string) => {
          window.location.href = url;
        });
      go(authorize_url);
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      type="button"
      className="connector-form__oauth-btn"
      data-provider={provider}
      data-testid={needsReauth ? "connector-oauth-reconnect" : "connector-oauth-connect"}
      disabled={busy}
      onClick={handleClick}
    >
      {needsReauth
        ? `Reconnect with ${titleCase(provider)}`
        : `Connect with ${titleCase(provider)}`}
    </button>
  );
}
