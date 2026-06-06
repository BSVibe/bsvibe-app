/**
 * ConnectorOAuthButton — the "Connect with X" control for oauth-method
 * connectors (Lift 0 skeleton). Not-connected → button that starts the OAuth
 * dance and redirects; connected → identity label, no button.
 */

import { ConnectorOAuthButton } from "@/components/settings/ConnectorOAuthButton";
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

  it("shows the connected identity and no button when connected", () => {
    render(<ConnectorOAuthButton provider="github" connectedLabel="@octocat" />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText(/@octocat/)).toBeTruthy();
  });
});
