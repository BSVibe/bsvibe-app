/**
 * Sentry connect (claim-later) in the Connectors catalog.
 *
 * Sentry uses an install→grant flow, not the standard OAuth start/callback:
 *  - Connect → fetch the external-install URL and navigate the browser there
 *    (when the operator has configured creds+slug).
 *  - Unconfigured → show the operator paste-creds form (with the slug field).
 *  - Installs land "unclaimed" (no workspace binding); a "Pending installs"
 *    section lets the founder claim one to this workspace.
 */

import Connectors from "@/components/settings/Connectors";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor, within } from "@testing-library/react";
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
  startConnectorOAuth: vi.fn(),
  setProviderAppCredentials: vi.fn(async () => ({ provider: "sentry", configured: true })),
  getSentryInstallUrl: vi.fn(async () => ({
    configured: true,
    install_url: "https://sentry.io/sentry-apps/bsvibe-app/external-install/",
  })),
  listUnclaimedInstalls: vi.fn(async () => ({ unclaimed: [] })),
  claimInstall: vi.fn(async () => ({ connector: "sentry", claimed: true })),
}));

import {
  claimInstall,
  getSentryInstallUrl,
  listUnclaimedInstalls,
  setProviderAppCredentials,
} from "@/lib/api/connectors";

const mockedInstallUrl = vi.mocked(getSentryInstallUrl);
const mockedListUnclaimed = vi.mocked(listUnclaimedInstalls);
const mockedClaim = vi.mocked(claimInstall);

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const realLocation = window.location;
let assignMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  clearSession();
  setSession(SESSION);
  assignMock = vi.fn();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...realLocation, assign: assignMock, href: realLocation.href },
  });
});

afterEach(() => {
  vi.clearAllMocks();
  Object.defineProperty(window, "location", { configurable: true, value: realLocation });
});

async function sentryCard(): Promise<HTMLElement> {
  const available = await screen.findByRole("list", { name: /available/i });
  // The AVAILABLE <ul> renders immediately (empty) while the catalog fetch is
  // still in flight, so findByRole resolves before the cards exist. findByText
  // retries until the async catalog populates the card — a plain getByText here
  // races the fetch and flakes under slower (CI) scheduling.
  const name = await within(available).findByText("Sentry");
  return name.closest("li") as HTMLElement;
}

describe("Sentry connect (claim-later)", () => {
  it("Connect navigates to the install URL when configured", async () => {
    render(<Connectors />);
    const card = await sentryCard();
    await userEvent.click(within(card).getByRole("button", { name: /connect/i }));
    await waitFor(() =>
      expect(assignMock).toHaveBeenCalledWith(
        "https://sentry.io/sentry-apps/bsvibe-app/external-install/",
      ),
    );
  });

  it("shows the operator creds form (with slug) when unconfigured", async () => {
    mockedInstallUrl.mockResolvedValueOnce({ configured: false, install_url: null });
    render(<Connectors />);
    const card = await sentryCard();
    await userEvent.click(within(card).getByRole("button", { name: /connect/i }));
    expect(await screen.findByLabelText(/integration slug/i)).toBeInTheDocument();
    expect(assignMock).not.toHaveBeenCalled();
  });

  it("after saving creds, fetches install URL and redirects", async () => {
    mockedInstallUrl
      .mockResolvedValueOnce({ configured: false, install_url: null })
      .mockResolvedValueOnce({
        configured: true,
        install_url: "https://sentry.io/sentry-apps/bsvibe-app/external-install/",
      });
    render(<Connectors />);
    const card = await sentryCard();
    await userEvent.click(within(card).getByRole("button", { name: /connect/i }));
    await userEvent.type(await screen.findByLabelText(/client id/i), "cid");
    await userEvent.type(screen.getByLabelText(/client secret/i), "sec");
    await userEvent.type(screen.getByLabelText(/integration slug/i), "bsvibe-app");
    await userEvent.click(screen.getByRole("button", { name: /save & connect/i }));
    await waitFor(() => {
      expect(setProviderAppCredentials).toHaveBeenCalledWith("sentry", "cid", "sec", "bsvibe-app");
      expect(assignMock).toHaveBeenCalledWith(
        "https://sentry.io/sentry-apps/bsvibe-app/external-install/",
      );
    });
  });

  it("lists pending installs and claims one to this workspace", async () => {
    mockedListUnclaimed.mockResolvedValue({
      unclaimed: [
        {
          id: "u-1",
          provider: "sentry",
          installation_ref: "inst-1",
          account_label: "Acme",
          created_at: "2026-06-06T00:00:00Z",
        },
      ],
    });
    render(<Connectors />);
    const pending = await screen.findByRole("list", { name: /pending installs/i });
    expect(within(pending).getByText(/Acme/)).toBeInTheDocument();
    await userEvent.click(
      within(pending).getByRole("button", { name: /connect to this workspace/i }),
    );
    await waitFor(() => expect(mockedClaim).toHaveBeenCalledWith("u-1"));
  });
});
