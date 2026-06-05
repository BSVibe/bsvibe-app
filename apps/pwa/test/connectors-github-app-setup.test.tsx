/**
 * GithubAppSetup (Lift 1.5) — the github card's three-state control:
 *   - App not set up  → "Set up GitHub App" → manifest form auto-submits to GitHub
 *   - App set up, not connected → "Connect with GitHub" (delegates to ConnectorOAuthButton)
 *   - connected → "Connected as @login"
 *
 * Prop-driven (parents supply `configured`) so it stays pure + testable; the
 * manifest start + form submit are injectable.
 */

import { GithubAppSetup } from "@/components/settings/GithubAppSetup";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api/connectors", () => ({
  startConnectorOAuth: vi.fn(),
  startGithubAppManifest: vi.fn(),
  getGithubAppStatus: vi.fn(),
}));

afterEach(() => vi.clearAllMocks());

describe("GithubAppSetup", () => {
  it("renders 'Set up GitHub App' when the App is not configured", () => {
    render(<GithubAppSetup configured={false} />);
    expect(screen.getByRole("button", { name: /set up github app/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /connect with github/i })).toBeNull();
  });

  it("auto-submits the manifest form to GitHub on setup click", async () => {
    const startManifest = vi.fn().mockResolvedValue({
      post_url: "https://github.com/settings/apps/new?state=st",
      manifest: { url: "https://app.bsvibe.dev" },
    });
    const submitManifestForm = vi.fn();
    render(
      <GithubAppSetup
        configured={false}
        startManifest={startManifest}
        submitManifestForm={submitManifestForm}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /set up github app/i }));

    await waitFor(() => {
      expect(startManifest).toHaveBeenCalledTimes(1);
      expect(submitManifestForm).toHaveBeenCalledWith(
        "https://github.com/settings/apps/new?state=st",
        { url: "https://app.bsvibe.dev" },
      );
    });
  });

  it("renders 'Connect with GitHub' when configured but not connected", () => {
    render(<GithubAppSetup configured={true} />);
    expect(screen.getByRole("button", { name: /connect with github/i })).toBeInTheDocument();
  });

  it("renders the connected identity when connected", () => {
    render(<GithubAppSetup configured={true} connectedLabel="@octocat" />);
    expect(screen.getByText(/@octocat/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
