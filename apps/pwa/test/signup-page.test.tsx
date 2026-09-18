/**
 * Signup page — the two outcomes the backend can return, and the refusal.
 *
 * The whole point of this page is that signup has TWO success shapes and they
 * must NOT look alike:
 *
 *   • live session      → the user is in; navigate to the app
 *   • confirmation sent → the user is NOT in; show "check your mail"
 *
 * Treating the second as a sign-in would send someone into the app whose
 * address is unproven and for whom the backend created no user row.
 *
 * And the refusal matters as much: this ships with email signup DISABLED in
 * the Supabase project, so `signUp` rejecting with 403 is the DEFAULT path
 * today, not an edge case. It has to read as a closed door, not a crash.
 */

import SignupPage from "@/app/signup/page";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/signup",
  useSearchParams: () => new URLSearchParams(),
}));

const signUp = vi.fn();
vi.mock("@/lib/api/auth", () => ({
  signUp: (...args: unknown[]) => signUp(...args),
}));

async function fillAndSubmit(): Promise<void> {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText(/email/i), "new@bsvibe.dev");
  // Anchored: the show/hide toggle's aria-label ("Show password") also
  // matches a loose /password/i.
  await user.type(screen.getByLabelText(/^password$/i), "pw-12345678");
  await user.click(screen.getByRole("button", { name: /create account|계정 만들기/i }));
}

describe("SignupPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("sends the typed credentials to signUp", async () => {
    signUp.mockResolvedValue({ confirmationRequired: true });
    render(<SignupPage />);
    await fillAndSubmit();
    await waitFor(() => expect(signUp).toHaveBeenCalledWith("new@bsvibe.dev", "pw-12345678"));
  });

  it("navigates into the app when a live session came back", async () => {
    signUp.mockResolvedValue({ confirmationRequired: false });
    render(<SignupPage />);
    await fillAndSubmit();
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/brief"));
  });

  it("shows 'check your mail' and does NOT navigate while confirmation is pending", async () => {
    signUp.mockResolvedValue({ confirmationRequired: true });
    render(<SignupPage />);
    await fillAndSubmit();

    // The positive half: the confirmation state is actually rendered.
    expect(await screen.findByRole("heading", { name: /mail|메일/i })).toBeTruthy();
    // The half that matters: nobody was sent into the app.
    expect(replace).not.toHaveBeenCalled();
  });

  it("shows a legible message when signups are disabled (the shipped default)", async () => {
    signUp.mockRejectedValue(new Error("signup is not available for this instance"));
    render(<SignupPage />);
    await fillAndSubmit();

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(replace).not.toHaveBeenCalled();
  });

  it("offers a way back to sign-in", async () => {
    render(<SignupPage />);
    const link = screen.getByRole("link", { name: /sign in|로그인/i });
    expect(link.getAttribute("href")).toBe("/login");
  });

  // ── the reason must survive to the screen ──────────────────────────────────
  //
  // The first version matched `/not available|403|signup/i` on the message —
  // and "signup" appears in almost every error this endpoint can produce, so
  // nearly everything read as "sign-up isn't open". That is the same defect the
  // backend had (PR #1004), mirrored on the client.

  it("shows the closed-door message ONLY for a real 403", async () => {
    const { ApiError } = await import("@/lib/api/client");
    signUp.mockRejectedValue(new ApiError(403, "POST /api/auth/signup → 403"));
    render(<SignupPage />);
    await fillAndSubmit();
    expect((await screen.findByRole("alert")).textContent).toMatch(/not open|열려 있지 않/i);
  });

  it("does NOT claim the door is closed on a rate limit", async () => {
    const { ApiError } = await import("@/lib/api/client");
    signUp.mockRejectedValue(new ApiError(429, "POST /api/auth/signup → 429"));
    render(<SignupPage />);
    await fillAndSubmit();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).not.toMatch(/not open|열려 있지 않/i);
  });

  it("tells the user to fix their input on a 400", async () => {
    const { ApiError } = await import("@/lib/api/client");
    signUp.mockRejectedValue(new ApiError(400, "POST /api/auth/signup → 400"));
    render(<SignupPage />);
    await fillAndSubmit();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).not.toMatch(/not open|열려 있지 않/i);
  });
});
