/**
 * Settings tab content wiring — smoke tests that each tab target renders the
 * right body:
 *
 *  - Models     → the EXISTING <ModelAccounts/> (unchanged by this lift)
 *  - Connectors → the EXISTING <Connectors/> (unchanged by this lift)
 *  - Notifications → the real notification-prefs surface (events × channels)
 *  - Account → a real route target with a "Coming soon" stub body
 *
 * Models/Connectors fetch on mount; we stub fetch with an empty list so the
 * surfaces reach their calm empty state without a network call. The
 * Notifications surface fetches its prefs object, so that test stubs a prefs
 * response instead of the empty list.
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

  it("Notifications tab renders the events × channels matrix", async () => {
    const prefs = {
      matrix: {
        needs_you: { in_app: true, email: true, slack: true },
        triggered: { in_app: true, email: true, slack: false },
        shipped: { in_app: true, email: true, slack: false },
        failed: { in_app: true, email: true, slack: false },
        daily_brief: { in_app: false, email: true, slack: false },
      },
      quiet_hours_enabled: false,
      quiet_hours_start: "22:00",
      quiet_hours_end: "08:00",
    };
    global.fetch = vi.fn(async () => jsonResponse(prefs)) as unknown as typeof fetch;
    render(<NotificationsTab />);
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: /events × channels/i })).toBeInTheDocument();
    });
    expect(screen.getByRole("heading", { name: /quiet hours/i })).toBeInTheDocument();
  });

  it("Account tab is a Coming soon stub", () => {
    render(<AccountTab />);
    expect(screen.getByRole("heading", { name: /account/i })).toBeInTheDocument();
    expect(screen.getByText(/coming soon/i)).toBeInTheDocument();
  });
});
