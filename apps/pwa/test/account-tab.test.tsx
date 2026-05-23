/**
 * Account tab — the real surface. Four sections per the design:
 *
 *  - Profile: avatar initials + email (real, from session), display name (from
 *    JWT user_metadata.name or the email local-part). Editing is a disabled
 *    "coming soon" affordance (no Supabase write in this lift).
 *  - Plan: a clearly non-functional placeholder card; Manage billing is disabled
 *    (Stripe deferred).
 *  - Sign-in identities: the provider(s) in the JWT app_metadata.providers show
 *    "Connected"; the other named providers show a DISABLED "Connect" (OAuth
 *    identity linking needs a backend — deferred).
 *  - Active sessions: the CURRENT session only ("This device") with a REAL sign
 *    out wired to the same `logout()` path AccountChip uses. Listing other
 *    devices / remote sign-out is disabled (needs Supabase admin — deferred).
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

describe("Account tab — Plan", () => {
  it("renders a plan placeholder card", () => {
    render(<AccountTab />);
    expect(screen.getByRole("heading", { name: /^plan$/i })).toBeInTheDocument();
  });

  it("disables Manage billing (Stripe deferred)", () => {
    render(<AccountTab />);
    expect(screen.getByRole("button", { name: /manage billing/i })).toBeDisabled();
  });
});

describe("Account tab — Sign-in identities", () => {
  it("marks the logged-in provider as Connected", () => {
    render(<AccountTab />);
    const google = screen.getByText(/google/i).closest("li");
    expect(google).not.toBeNull();
    expect(within(google as HTMLElement).getByText(/connected/i)).toBeInTheDocument();
  });

  it("shows other providers with a disabled Connect (linking deferred)", () => {
    render(<AccountTab />);
    const github = screen.getByText(/github/i).closest("li");
    expect(github).not.toBeNull();
    expect(within(github as HTMLElement).getByRole("button", { name: /connect/i })).toBeDisabled();
  });
});

describe("Account tab — Active sessions", () => {
  it("shows a 'This device' row for the current session", () => {
    render(<AccountTab />);
    expect(screen.getByText(/this device/i)).toBeInTheDocument();
  });

  it("signs out via the same logout() path AccountChip uses", async () => {
    const user = userEvent.setup();
    render(<AccountTab />);
    await user.click(screen.getByRole("button", { name: /^sign out$/i }));
    expect(logoutMock).toHaveBeenCalledTimes(1);
    expect(replaceMock).toHaveBeenCalledWith("/login");
  });
});
