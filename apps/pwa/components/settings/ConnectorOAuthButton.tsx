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

import { ApiError } from "@/lib/api/client";
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
  const [error, setError] = useState<string | null>(null);

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
    setError(null);
    try {
      const { authorize_url } = await startConnectorOAuth(provider);
      const go =
        onRedirect ??
        ((url: string) => {
          window.location.href = url;
        });
      go(authorize_url);
    } catch (e) {
      // F10 — a connector whose OAuth app isn't configured in prod 404s on the
      // start route; surface it instead of failing silently (the click looked
      // like a no-op). A 404 means "not wired up yet" — distinct from a
      // transient failure the user can retry.
      const status = e instanceof ApiError ? e.status : 0;
      setError(
        status === 404
          ? `${titleCase(provider)} isn't available yet — its connection hasn't been set up.`
          : `Couldn't start the ${titleCase(provider)} connection. Please try again.`,
      );
    } finally {
      setBusy(false);
    }
  };

  const idleLabel = needsReauth
    ? `Reconnect with ${titleCase(provider)}`
    : `Connect with ${titleCase(provider)}`;

  return (
    <div className="connector-form__oauth">
      <button
        type="button"
        className="connector-form__oauth-btn"
        data-provider={provider}
        data-testid={needsReauth ? "connector-oauth-reconnect" : "connector-oauth-connect"}
        disabled={busy}
        aria-busy={busy}
        onClick={handleClick}
      >
        {busy ? (
          // The start round-trip has latency, then the browser navigates to the
          // provider. Without visible feedback the click reads as a no-op and
          // users re-click (observed: 3× rapid oauth/start). Announce the work.
          <>
            <span className="connector-form__oauth-spinner" aria-hidden="true" />
            Connecting…
          </>
        ) : (
          idleLabel
        )}
      </button>
      {error && (
        <p className="connector-form__oauth-error" role="alert" data-provider={provider}>
          {error}
        </p>
      )}
    </div>
  );
}
