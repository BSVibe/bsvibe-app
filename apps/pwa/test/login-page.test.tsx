/**
 * Login page — centered-card redesign behaviours: password show/hide toggle,
 * social sign-in buttons (Google / GitHub), the "Forgot password?" link, and
 * the success / loading / error states of the email+password submit.
 *
 * `@/lib/api/auth` and `next/navigation` are mocked so the page is exercised in
 * isolation with no real backend.
 */

import LoginPage from "@/app/login/page";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const replace = vi.fn();
let searchParams = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/login",
  useSearchParams: () => searchParams,
}));

const login = vi.fn();
const startOAuth = vi.fn();
vi.mock("@/lib/api/auth", () => ({
  login: (...args: unknown[]) => login(...args),
  startOAuth: (...args: unknown[]) => startOAuth(...args),
  RETURN_TO_KEY: "bsvibe.return_to",
  isSameOriginPath: (raw: string | null | undefined): boolean =>
    !!raw && raw.startsWith("/") && !raw.startsWith("//"),
}));

describe("LoginPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    searchParams = new URLSearchParams();
    sessionStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    sessionStorage.clear();
  });

  it("renders the brand, social buttons, and email form", () => {
    render(<LoginPage />);
    expect(screen.getByText("AI Agent OS")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue with Google" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue with GitHub" })).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
  });

  it("toggles password visibility", async () => {
    render(<LoginPage />);
    const password = screen.getByLabelText("Password") as HTMLInputElement;
    expect(password.type).toBe("password");

    await userEvent.click(screen.getByRole("button", { name: "Show password" }));
    expect(password.type).toBe("text");

    await userEvent.click(screen.getByRole("button", { name: "Hide password" }));
    expect(password.type).toBe("password");
  });

  it("has a forgot-password link to /forgot-password", () => {
    render(<LoginPage />);
    const link = screen.getByRole("link", { name: "Forgot password?" });
    expect(link).toHaveAttribute("href", "/forgot-password");
  });

  it("starts Google sign-in when the social button is clicked", async () => {
    startOAuth.mockResolvedValue(undefined);
    render(<LoginPage />);
    await userEvent.click(screen.getByRole("button", { name: "Continue with Google" }));
    // Default return_to is /brief → pass `undefined` so the login page
    // doesn't bake the boring default into the IdP callback URL.
    expect(startOAuth).toHaveBeenCalledWith("google", undefined);
  });

  it("forwards return_to into startOAuth when one is present", async () => {
    searchParams = new URLSearchParams(`return_to=${encodeURIComponent("/oauth/consent?x=1")}`);
    startOAuth.mockResolvedValue(undefined);
    render(<LoginPage />);
    await userEvent.click(screen.getByRole("button", { name: "Continue with Google" }));
    expect(startOAuth).toHaveBeenCalledWith("google", "/oauth/consent?x=1");
  });

  it("logs in and routes to /brief on success", async () => {
    login.mockResolvedValue(undefined);
    render(<LoginPage />);
    await userEvent.type(screen.getByLabelText("Email"), "founder@bsvibe.dev");
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await waitFor(() => expect(login).toHaveBeenCalledWith("founder@bsvibe.dev", "pw"));
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/brief"));
  });

  it("routes to ?return_to= on success when one is provided", async () => {
    // The OAuth consent page bounces unauthenticated visitors here with
    // a return_to that lands them back on the consent screen.
    searchParams = new URLSearchParams(
      `return_to=${encodeURIComponent("/oauth/consent?client_id=dcr-abc")}`,
    );
    login.mockResolvedValue(undefined);
    render(<LoginPage />);
    await userEvent.type(screen.getByLabelText("Email"), "founder@bsvibe.dev");
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/oauth/consent?client_id=dcr-abc"));
  });

  it("rejects an external return_to to prevent open redirect", async () => {
    searchParams = new URLSearchParams("return_to=https://evil.com/steal");
    login.mockResolvedValue(undefined);
    render(<LoginPage />);
    await userEvent.type(screen.getByLabelText("Email"), "founder@bsvibe.dev");
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/brief"));
  });

  it("shows an error and re-enables submit when login fails", async () => {
    login.mockRejectedValue(new Error("nope"));
    render(<LoginPage />);
    await userEvent.type(screen.getByLabelText("Email"), "founder@bsvibe.dev");
    await userEvent.type(screen.getByLabelText("Password"), "wrong");
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    expect(
      await screen.findByText("Couldn’t sign you in. Check your email and password."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled();
    expect(replace).not.toHaveBeenCalled();
  });
});
