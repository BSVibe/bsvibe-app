/**
 * Account tab — the real, HONEST surface (L6 §4 cleanup). Three sections:
 *
 *  - Profile: avatar initials + email (real, from session), display name (from
 *    JWT user_metadata.name or the email local-part). "Change photo" stays a
 *    disabled "coming soon" affordance (no Supabase write in this lift).
 *  - Sign-in identities: READ-ONLY. Only the provider(s) actually present in the
 *    JWT render as "Signed in with X" — no Connect/Disconnect buttons (no backend
 *    identity linking, so the dead controls were removed).
 *  - Active sessions: the CURRENT session only ("This device") with a REAL sign
 *    out wired to the same `logout()` path AccountChip uses. No "other devices
 *    coming soon" note (no remote-session listing backend).
 *
 *  Plan/billing is hidden entirely until billing is real.
 */

import AccountTab from "@/components/settings/AccountTab";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const logoutMock = vi.fn(async () => {});
const replaceMock = vi.fn();

vi.mock("@/lib/api/auth", () => ({
  logout: () => logoutMock(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, push: vi.fn(), prefetch: vi.fn() }),
}));

/** Build a JWT-shaped token whose payload base64url-encodes `payload`. */
function makeToken(payload: Record<string, unknown>): string {
  const b64url = (obj: unknown) =>
    Buffer.from(JSON.stringify(obj))
      .toString("base64")
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
  return `${b64url({ alg: "ES256", typ: "JWT" })}.${b64url(payload)}.sig`;
}

const ACCESS_TOKEN = makeToken({
  email: "alex@bsvibe.dev",
  app_metadata: { providers: ["google"], role: "authenticated" },
  user_metadata: { name: "Alex Chen" },
});

const SESSION: Session = {
  accessToken: ACCESS_TOKEN,
  refreshToken: "ref",
  email: "alex@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
  personalAccountId: "acct-abc-123",
};

beforeEach(() => {
  clearSession();
  setSession(SESSION);
  logoutMock.mockClear();
  replaceMock.mockClear();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("Account tab — Profile", () => {
  it("shows the signed-in email", () => {
    render(<AccountTab />);
    expect(screen.getByText("alex@bsvibe.dev")).toBeInTheDocument();
  });

  it("shows the display name from the JWT user_metadata.name", () => {
    render(<AccountTab />);
    expect(screen.getByText("Alex Chen")).toBeInTheDocument();
  });

  it("renders an avatar with the initials placeholder", () => {
    render(<AccountTab />);
    // Initials from the name ("Alex Chen" → "AC") rendered in the avatar.
    expect(screen.getByText("AC")).toBeInTheDocument();
  });

  it("offers a Change photo control that is disabled (coming soon)", () => {
    render(<AccountTab />);
    const change = screen.getByRole("button", { name: /change photo/i });
    expect(change).toBeDisabled();
  });
});

describe("Account tab — Plan (L6 §4 — hidden until billing is real)", () => {
  it("does NOT render a Plan section", () => {
    render(<AccountTab />);
    expect(screen.queryByRole("heading", { name: /^plan$/i })).toBeNull();
  });

  it("does NOT render a Manage billing control", () => {
    render(<AccountTab />);
    expect(screen.queryByRole("button", { name: /manage billing/i })).toBeNull();
  });
});

describe("Account tab — Sign-in identities (L6 §4 — read-only, no dead controls)", () => {
  it("marks the logged-in provider as signed in (read-only)", () => {
    render(<AccountTab />);
    const google = screen.getByText(/google/i).closest("li");
    expect(google).not.toBeNull();
    expect(within(google as HTMLElement).getByText(/connected/i)).toBeInTheDocument();
  });

  it("renders NO Connect/Disconnect identity buttons (no backend linking)", () => {
    render(<AccountTab />);
    const identities = screen.getByRole("region", { name: /sign-in identities/i });
    expect(within(identities).queryByRole("button")).toBeNull();
  });

  it("does NOT list providers that aren't in the JWT (github not signed in)", () => {
    render(<AccountTab />);
    const identities = screen.getByRole("region", { name: /sign-in identities/i });
    expect(within(identities).queryByText(/github/i)).toBeNull();
  });

  it("shows an empty-state note for a password-only account (finding A-8)", () => {
    // No linkable OAuth providers in the JWT → the section must not render as a
    // bare heading with an empty list; it explains the email/password sign-in.
    clearSession();
    setSession({
      ...SESSION,
      accessToken: makeToken({
        email: "alex@bsvibe.dev",
        app_metadata: { providers: ["email"], role: "authenticated" },
      }),
    });
    render(<AccountTab />);
    const identities = screen.getByRole("region", { name: /sign-in identities/i });
    expect(within(identities).queryByText(/connected/i)).toBeNull();
    expect(within(identities).getByText(/email and password/i)).toBeInTheDocument();
  });
});

describe("Account tab — Active sessions", () => {
  it("shows a 'This device' row for the current session", () => {
    render(<AccountTab />);
    expect(screen.getByText(/this device/i)).toBeInTheDocument();
  });

  it("does NOT render an 'other devices coming soon' note (L6 §4)", () => {
    render(<AccountTab />);
    const sessions = screen.getByRole("region", { name: /active sessions/i });
    expect(within(sessions).queryByText(/other devices/i)).toBeNull();
    expect(within(sessions).queryByText(/coming soon/i)).toBeNull();
  });

  it("signs out via the same logout() path AccountChip uses", async () => {
    const user = userEvent.setup();
    render(<AccountTab />);
    await user.click(screen.getByRole("button", { name: /^sign out$/i }));
    expect(logoutMock).toHaveBeenCalledTimes(1);
    expect(replaceMock).toHaveBeenCalledWith("/login");
  });
});
