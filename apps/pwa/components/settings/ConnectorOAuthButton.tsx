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
  onRedirect,
}: {
  provider: string;
  /** When set, the connector is already connected — show the identity. */
  connectedLabel?: string | null;
  /** Override the post-start navigation (tests). */
  onRedirect?: (url: string) => void;
}) {
  const [busy, setBusy] = useState(false);

  if (connectedLabel) {
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
      disabled={busy}
      onClick={handleClick}
    >
      Connect with {titleCase(provider)}
    </button>
  );
}
