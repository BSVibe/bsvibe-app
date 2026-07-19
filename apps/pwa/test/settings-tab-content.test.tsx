/**
 * Settings tab content wiring — smoke tests that each tab target renders the
 * right body:
 *
 *  - Models     → the EXISTING <ModelAccounts/> (unchanged by this lift)
 *  - Connectors → the EXISTING <Connectors/> (unchanged by this lift)
 *  - Notifications → the real notification-prefs surface (events × channels)
 *  - Account → the real Profile / Plan / identities / sessions surface
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

// AccountTab surfaces a real sign-out (router.replace + logout()); stub both so
// the smoke render doesn't need real navigation or a network call.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
}));

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

  it("Notifications tab renders the real events × channels matrix (delivery is wired)", async () => {
    // Delivery is live (N2a/N3), so the tab fetches prefs and renders the grid.
    // Stub a prefs response with a bound telegram push channel.
    global.fetch = vi.fn(async () =>
      jsonResponse({
        matrix: {
          needs_you: { in_app: true },
          triggered: { in_app: true },
          shipped: { in_app: true },
          failed: { in_app: true },
          daily_brief: { in_app: false },
        },
        quiet_hours_enabled: false,
        quiet_hours_start: "22:00",
        quiet_hours_end: "08:00",
        available_channels: ["in_app", "telegram"],
      }),
    ) as unknown as typeof fetch;
    render(<NotificationsTab />);
    // Async: the grid appears only after the on-mount fetch resolves (findBy*).
    expect(await screen.findByRole("heading", { name: /events × channels/i })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: /quiet hours/i })).toBeInTheDocument();
  });

  it("Account tab renders the real Profile / identities / sessions surface (L6 §4 — no Plan)", () => {
    render(<AccountTab />);
    // The signed-in email (real, from session) anchors the Profile section.
    expect(screen.getByText("founder@bsvibe.dev")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /profile/i })).toBeInTheDocument();
    // Plan/billing is hidden until billing is real.
    expect(screen.queryByRole("heading", { name: /^plan$/i })).toBeNull();
    expect(screen.getByRole("heading", { name: /sign-in identities/i })).toBeInTheDocument();
    expect(screen.getByText(/this device/i)).toBeInTheDocument();
  });
});
