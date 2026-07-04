/**
 * ConnectorOAuthButton — the "Connect with X" control for oauth-method
 * connectors (Lift 0 skeleton). Not-connected → button that starts the OAuth
 * dance and redirects; connected → identity label, no button.
 */

import { ConnectorOAuthButton } from "@/components/settings/ConnectorOAuthButton";
import { ApiError } from "@/lib/api/client";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api/connectors", () => ({
  startConnectorOAuth: vi.fn(),
}));

import { startConnectorOAuth } from "@/lib/api/connectors";

const mockedStart = vi.mocked(startConnectorOAuth);

afterEach(() => {
  vi.clearAllMocks();
});

describe("ConnectorOAuthButton", () => {
  it("renders a Connect button when not connected", () => {
    render(<ConnectorOAuthButton provider="github" />);
    const btn = screen.getByRole("button");
    expect(btn.textContent).toMatch(/connect/i);
    expect(btn.textContent).toMatch(/github/i);
  });

  it("starts the dance and redirects on click", async () => {
    mockedStart.mockResolvedValue({ authorize_url: "https://provider.example/authorize?s=1" });
    const onRedirect = vi.fn();
    render(<ConnectorOAuthButton provider="github" onRedirect={onRedirect} />);

    fireEvent.click(screen.getByRole("button"));

    await waitFor(() => {
      expect(mockedStart).toHaveBeenCalledWith("github");
      expect(onRedirect).toHaveBeenCalledWith("https://provider.example/authorize?s=1");
    });
  });

  it("shows a Connecting… busy state between click and redirect (no perceived freeze)", async () => {
    // The start round-trip has latency; without visible feedback the click
    // looks like a no-op and users re-click (observed: 3× rapid oauth/start).
    // While the dance is in flight the button must announce it's working:
    // label flips to Connecting…, aria-busy is set, and it stays disabled.
    let resolveStart: (v: { authorize_url: string }) => void = () => {};
    mockedStart.mockReturnValue(
      new Promise((res) => {
        resolveStart = res;
      }),
    );
    const onRedirect = vi.fn();
    render(<ConnectorOAuthButton provider="github" onRedirect={onRedirect} />);

    const btn = screen.getByRole("button");
    fireEvent.click(btn);

    // In-flight: immediate, visible working feedback.
    await waitFor(() => {
      expect(btn.textContent).toMatch(/connecting/i);
      expect(btn).toHaveAttribute("aria-busy", "true");
      expect(btn).toBeDisabled();
    });

    resolveStart({ authorize_url: "https://provider.example/authorize?s=1" });
    await waitFor(() => {
      expect(onRedirect).toHaveBeenCalledWith("https://provider.example/authorize?s=1");
    });
  });

  it("shows the connected identity and no button when connected", () => {
    render(<ConnectorOAuthButton provider="github" connectedLabel="@octocat" />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText(/@octocat/)).toBeTruthy();
  });

  it("surfaces a clear error when the provider is not configured (404), no redirect", async () => {
    // F10 — clicking Connect on a connector whose OAuth app is not configured
    // in prod 404s; the failure must be visible, not a silent no-op.
    mockedStart.mockRejectedValue(new ApiError(404, "unknown or unregistered provider: slack"));
    const onRedirect = vi.fn();
    render(<ConnectorOAuthButton provider="slack" onRedirect={onRedirect} />);

    fireEvent.click(screen.getByRole("button"));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/slack/i);
    expect(alert.textContent).toMatch(/not available|isn.t available|not configured/i);
    expect(onRedirect).not.toHaveBeenCalled();
  });

  it("surfaces a generic retry error on a non-404 failure, no redirect", async () => {
    mockedStart.mockRejectedValue(new ApiError(500, "boom"));
    const onRedirect = vi.fn();
    render(<ConnectorOAuthButton provider="slack" onRedirect={onRedirect} />);

    fireEvent.click(screen.getByRole("button"));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/couldn.t|try again|failed/i);
    expect(onRedirect).not.toHaveBeenCalled();
  });
});
