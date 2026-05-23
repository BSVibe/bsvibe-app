/**
 * Settings tab content wiring — smoke tests that each tab target renders the
 * right body:
 *
 *  - Models     → the EXISTING <ModelAccounts/> (unchanged by this lift)
 *  - Connectors → the EXISTING <Connectors/> (unchanged by this lift)
 *  - Notifications / Account → real route targets with a "Coming soon" stub body
 *
 * Models/Connectors fetch on mount; we stub fetch with an empty list so the
 * surfaces reach their calm empty state without a network call.
 */

import AccountTab from "@/components/settings/AccountTab";
import ConnectorsTab from "@/components/settings/ConnectorsTab";
import ModelsTab from "@/components/settings/ModelsTab";
import NotificationsTab from "@/components/settings/NotificationsTab";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  clearSession();
  setSession(SESSION);
  global.fetch = vi.fn(async () => jsonResponse([])) as unknown as typeof fetch;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Settings tab content", () => {
  it("Models tab renders the model-accounts surface", async () => {
    render(<ModelsTab />);
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: /model accounts/i })).toBeInTheDocument();
    });
  });

  it("Connectors tab renders the connectors surface", async () => {
    render(<ConnectorsTab />);
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: /connectors/i })).toBeInTheDocument();
    });
  });

  it("Notifications tab is a Coming soon stub", () => {
    render(<NotificationsTab />);
    expect(screen.getByRole("heading", { name: /notifications/i })).toBeInTheDocument();
    expect(screen.getByText(/coming soon/i)).toBeInTheDocument();
  });

  it("Account tab is a Coming soon stub", () => {
    render(<AccountTab />);
    expect(screen.getByRole("heading", { name: /account/i })).toBeInTheDocument();
    expect(screen.getByText(/coming soon/i)).toBeInTheDocument();
  });
});
