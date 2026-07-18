/**
 * Connectors wiring — when a paste-creds OAuth provider (slack/notion/discord)
 * isn't configured, "Connect" opens the operator ProviderAppConfig form instead
 * of the calm "not available" note. github (manifest) still shows "not
 * available" (operator sets it up via the manifest flow, not a creds form).
 */

import Connectors from "@/components/settings/Connectors";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api/connectors", () => ({
  listConnectors: vi.fn(async () => []),
  getConnectorCatalog: vi.fn(async () => ({
    connectors: ["github", "slack", "telegram", "discord", "notion", "sentry", "obsidian"].map(
      (name) => ({
        name,
        outbound: true,
        importable: ["obsidian", "claude", "gpt", "notion"].includes(name),
        webhook_trigger: false,
        artifact_types: [],
        import_action: null,
      }),
    ),
  })),
  createConnector: vi.fn(),
  revokeConnector: vi.fn(),
  triggerImport: vi.fn(),
  startConnectorOAuth: vi.fn(async () => {
    throw new Error("provider not configured");
  }),
  setProviderAppCredentials: vi.fn(async () => ({ provider: "slack", configured: true })),
  getSentryInstallUrl: vi.fn(async () => ({ configured: false, install_url: null })),
  listUnclaimedInstalls: vi.fn(async () => ({ unclaimed: [] })),
  claimInstall: vi.fn(async () => ({ connector: "sentry", claimed: true })),
}));

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "f@bsvibe.dev",
  userId: "u1",
  expiresAt: Date.now() + 3_600_000,
};

beforeEach(() => {
  clearSession();
  setSession(SESSION);
});
afterEach(() => vi.clearAllMocks());

async function availableCard(name: string): Promise<HTMLElement> {
  const available = await screen.findByRole("list", { name: /available/i });
  // The AVAILABLE <ul> renders immediately (empty) while the catalog fetch is
  // still in flight, so findByRole resolves before the cards exist. findByText
  // retries until the async catalog populates the card — a plain getByText here
  // races the fetch and flakes under slower (CI) scheduling.
  const label = await within(available).findByText(name);
  return label.closest("li") as HTMLElement;
}

describe("Connectors — operator paste-creds wiring", () => {
  it("slack Connect (unconfigured) → opens the ProviderAppConfig form", async () => {
    render(<Connectors />);
    const card = await availableCard("Slack");
    await userEvent.click(within(card).getByRole("button", { name: /^Connect$/i }));

    expect(await screen.findByLabelText(/client id/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/client secret/i)).toBeInTheDocument();
  });

  it("github Connect (unconfigured) → 'not available' note, NO creds form", async () => {
    render(<Connectors />);
    const card = await availableCard("GitHub");
    await userEvent.click(within(card).getByRole("button", { name: /^Connect$/i }));

    expect(await screen.findByText(/not available/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/client secret/i)).toBeNull();
  });
});
