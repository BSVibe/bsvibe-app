"use client";

/**
 * GithubAppSetup — the github card's connect control (Lift 1.5). Three states:
 *
 *  - App NOT configured → "Set up GitHub App". Click → the backend mints a
 *    manifest, and we auto-submit it as a form POST to GitHub
 *    (settings/apps/new). GitHub creates the App and redirects back to the
 *    manifest callback, which stores the credentials — no manual env editing.
 *  - App configured, not connected → "Connect with GitHub" (the user-to-server
 *    OAuth dance via ConnectorOAuthButton).
 *  - connected → "Connected as @login".
 *
 * Status is resolved OPTIMISTICALLY: the control renders the Connect button
 * synchronously and only flips to "Set up GitHub App" once app-status reports
 * the App isn't configured. That avoids a loading flash and keeps the control
 * usable even if the status probe fails. Callers may pass `configured`
 * explicitly to skip the probe; the manifest start + form submit are injectable
 * for tests.
 */

import {
  type GithubAppManifestStart,
  type GithubAppStatus,
  getGithubAppStatus,
  startGithubAppManifest,
} from "@/lib/api/connectors";
import { useEffect, useState } from "react";
import { ConnectorOAuthButton } from "./ConnectorOAuthButton";

/** Build a hidden form and POST the manifest to GitHub (top-level navigation). */
function submitManifestFormToGithub(postUrl: string, manifest: Record<string, unknown>): void {
  const form = document.createElement("form");
  form.method = "POST";
  form.action = postUrl;
  const field = document.createElement("input");
  field.type = "hidden";
  field.name = "manifest";
  field.value = JSON.stringify(manifest);
  form.appendChild(field);
  document.body.appendChild(form);
  form.submit();
}

export function GithubAppSetup({
  configured,
  connectedLabel,
  getStatus = getGithubAppStatus,
  startManifest = startGithubAppManifest,
  submitManifestForm = submitManifestFormToGithub,
  onRedirect,
}: {
  /** Explicit override; when omitted the component probes app-status itself. */
  configured?: boolean;
  connectedLabel?: string | null;
  getStatus?: () => Promise<GithubAppStatus>;
  startManifest?: () => Promise<GithubAppManifestStart>;
  submitManifestForm?: (postUrl: string, manifest: Record<string, unknown>) => void;
  onRedirect?: (url: string) => void;
}) {
  const [probed, setProbed] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    // Skip the probe when the caller decided, or when already connected.
    if (configured !== undefined || connectedLabel) return;
    let active = true;
    getStatus()
      .then((s) => active && setProbed(s.configured))
      .catch(() => {
        /* keep the optimistic Connect on probe failure */
      });
    return () => {
      active = false;
    };
  }, [configured, connectedLabel, getStatus]);

  // Optimistic default true → render Connect synchronously; flip to Setup only
  // once the probe confirms the App isn't configured.
  const effectiveConfigured = configured ?? probed ?? true;

  if (connectedLabel || effectiveConfigured) {
    return (
      <ConnectorOAuthButton
        provider="github"
        connectedLabel={connectedLabel}
        onRedirect={onRedirect}
      />
    );
  }

  const handleSetup = async () => {
    setBusy(true);
    try {
      const { post_url, manifest } = await startManifest();
      submitManifestForm(post_url, manifest);
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      type="button"
      className="connector-form__oauth-btn"
      data-provider="github"
      data-action="setup"
      disabled={busy}
      onClick={handleSetup}
    >
      Set up GitHub App
    </button>
  );
}
