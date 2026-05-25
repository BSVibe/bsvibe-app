/**
 * /auth/callback — finishes social sign-in. On mount it reads the `?code=` the
 * provider redirected back with, exchanges it (PKCE verifier from sessionStorage
 * via completeOAuth), and routes to /brief. A missing code or a failed exchange
 * shows a calm error with a way back to sign in.
 */

import CallbackPage from "@/app/auth/callback/page";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => searchParams,
}));

const completeOAuth = vi.fn();
vi.mock("@/lib/api/auth", () => ({
  completeOAuth: (...args: unknown[]) => completeOAuth(...args),
  getPendingOAuthProvider: () => "google",
}));

let searchParams = new URLSearchParams();

describe("auth callback page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    searchParams = new URLSearchParams();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("exchanges the code and routes to /brief", async () => {
    searchParams = new URLSearchParams("code=auth-code-1");
    completeOAuth.mockResolvedValue(undefined);

    render(<CallbackPage />);

    await waitFor(() => expect(completeOAuth).toHaveBeenCalledWith("google", "auth-code-1"));
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/brief"));
  });

  it("shows an error with a back link when the exchange fails", async () => {
    searchParams = new URLSearchParams("code=bad-code");
    completeOAuth.mockRejectedValue(new Error("nope"));

    render(<CallbackPage />);

    expect(
      await screen.findByText("Sign-in couldn’t be completed. Please try again."),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to sign in" })).toHaveAttribute("href", "/login");
    expect(replace).not.toHaveBeenCalled();
  });

  it("shows an error when no code is present", async () => {
    render(<CallbackPage />);

    expect(
      await screen.findByText("Sign-in couldn’t be completed. Please try again."),
    ).toBeInTheDocument();
    expect(completeOAuth).not.toHaveBeenCalled();
  });
});
