/**
 * /auth/callback — finishes social sign-in. On mount it reads the `?code=` the
 * provider redirected back with, exchanges it (PKCE verifier from sessionStorage
 * via completeOAuth), and routes to /brief. A missing code or a failed exchange
 * shows a calm error with a way back to sign in.
 *
 * Lift E11 — the OAuth `return_to` round-trip now lives ENTIRELY in
 * sessionStorage under the `bsvibe.return_to` key. The legacy hash-fragment
 * encoding was provably unreliable through the IdP 302 chain (Supabase strips
 * or overwrites the fragment in practice — dogfood-reproduced 2026-06-06), so
 * we've dropped it. A malformed value (caller tried `https://evil.com`) MUST
 * surface a visible error instead of silently falling through to /brief.
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
  RETURN_TO_KEY: "bsvibe.return_to",
  isSameOriginPath: (raw: string | null | undefined): boolean =>
    !!raw && raw.startsWith("/") && !raw.startsWith("//"),
}));

let searchParams = new URLSearchParams();

describe("auth callback page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    searchParams = new URLSearchParams();
    sessionStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    sessionStorage.clear();
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

  it("honours sessionStorage return_to after a successful exchange", async () => {
    // This is the OAuth-consent round-trip: ConsentClient stashed a
    // return_to before bouncing through /login, startOAuth wrote it again
    // atomically just before handing off to the IdP. After Supabase brings
    // the user back, the callback page MUST land on the saved consent URL,
    // not on /brief.
    searchParams = new URLSearchParams("code=auth-code-1");
    sessionStorage.setItem(
      "bsvibe.return_to",
      "/oauth/consent?response_type=code&client_id=dcr-abc&redirect_uri=http%3A%2F%2F127.0.0.1%3A53113%2F&state=XYZ",
    );
    completeOAuth.mockResolvedValue(undefined);

    render(<CallbackPage />);

    await waitFor(() => expect(completeOAuth).toHaveBeenCalled());
    await waitFor(() =>
      expect(replace).toHaveBeenCalledWith(
        "/oauth/consent?response_type=code&client_id=dcr-abc&redirect_uri=http%3A%2F%2F127.0.0.1%3A53113%2F&state=XYZ",
      ),
    );
    // Single-use: the key is cleared after consumption so a subsequent
    // vanilla sign-in doesn't accidentally inherit the prior flow's URL.
    expect(sessionStorage.getItem("bsvibe.return_to")).toBeNull();
  });

  it("surfaces a visible error when the stashed return_to is unsafe", async () => {
    // The /login page already guards against open-redirect return_to values
    // before stashing, so this should be unreachable in practice. But if a
    // crafted page (or a regression) ever stashed `https://evil.com`, we MUST
    // NOT silently fall through to /brief — that's the failure mode that
    // hid the original Lift E4 bug. Surface "OAuth context lost" instead.
    searchParams = new URLSearchParams("code=auth-code-1");
    sessionStorage.setItem("bsvibe.return_to", "https://evil.com/steal");
    completeOAuth.mockResolvedValue(undefined);

    render(<CallbackPage />);

    expect(
      await screen.findByText("OAuth flow lost context — please run `bsvibe login` again."),
    ).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
    // Hostile value cleared so a manual retry starts clean.
    expect(sessionStorage.getItem("bsvibe.return_to")).toBeNull();
  });

  it("falls back to /brief when no return_to is stashed (vanilla sign-in)", async () => {
    // The everyday case: founder clicks "Continue with Google" straight from
    // /login with no return_to in the URL. No sessionStorage entry was ever
    // written, so completion lands on /brief — no error, no warning.
    searchParams = new URLSearchParams("code=auth-code-1");
    completeOAuth.mockResolvedValue(undefined);

    render(<CallbackPage />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/brief"));
  });
});
