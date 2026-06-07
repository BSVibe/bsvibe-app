/**
 * OAuth connectors connect in ONE click, no modal (SaaS single-app model).
 *
 * Clicking "Connect" on an OAuth connector card (github/slack/notion/discord)
 * goes STRAIGHT to the provider authorize URL — no AddConnector modal (those
 * connectors need no form input; the app is operator-configured once). Secret /
 * path connectors (telegram/obsidian/…) still open the modal for their fields.
 * An unconfigured OAuth provider degrades to a calm "not available" note
 * instead of crashing.
 */

import Connectors from "@/components/settings/Connectors";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api/connectors", () => ({
  listConnectors: vi.fn(async () => []),
  createConnector: vi.fn(),
  revokeConnector: vi.fn(),
  triggerImport: vi.fn(),
  startConnectorOAuth: vi.fn(async () => ({
    authorize_url: "https://github.com/login/oauth/authorize?client_id=x",
  })),
  setProviderAppCredentials: vi.fn(async () => ({ provider: "slack", configured: true })),
}));

import { startConnectorOAuth } from "@/lib/api/connectors";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const mockedStart = vi.mocked(startConnectorOAuth);

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

async function availableCard(name: string): Promise<HTMLElement> {
  const available = await screen.findByRole("list", { name: /available/i });
  return within(available).getByText(name).closest("li") as HTMLElement;
}

describe("OAuth 1-click connect", () => {
  it("github Connect → startConnectorOAuth + redirect, NO modal", async () => {
    render(<Connectors />);

    const card = await availableCard("GitHub");
    await userEvent.click(within(card).getByRole("button", { name: /^Connect$/i }));

    await waitFor(() => expect(mockedStart).toHaveBeenCalledWith("github"));
    await waitFor(() =>
      expect(assignMock).toHaveBeenCalledWith(
        "https://github.com/login/oauth/authorize?client_id=x",
      ),
    );
    // The AddConnector modal (which has a "Connector" picker) never opened.
    expect(screen.queryByLabelText("Connector")).toBeNull();
  });

  it("secret connector (telegram) Connect → opens the modal, no OAuth", async () => {
    render(<Connectors />);

    const card = await availableCard("Telegram");
    await userEvent.click(within(card).getByRole("button", { name: /^Connect$/i }));

    expect(await screen.findByLabelText("Connector")).toBeInTheDocument();
    expect(mockedStart).not.toHaveBeenCalled();
  });

  it("unconfigured github (manifest provider) → calm note, no redirect, no crash", async () => {
    // github is operator-set via the manifest flow (not paste-creds), so an
    // unconfigured github degrades to the calm note (slack/notion/discord open
    // the paste-creds form instead — see connectors-operator-wiring).
    mockedStart.mockRejectedValueOnce(new Error("provider not configured"));
    render(<Connectors />);

    const card = await availableCard("GitHub");
    await userEvent.click(within(card).getByRole("button", { name: /^Connect$/i }));

    expect(await screen.findByText(/not available/i)).toBeInTheDocument();
    expect(assignMock).not.toHaveBeenCalled();
    // Surface stays intact (heading still there).
    expect(screen.getByRole("heading", { name: /connectors/i })).toBeInTheDocument();
  });
});
