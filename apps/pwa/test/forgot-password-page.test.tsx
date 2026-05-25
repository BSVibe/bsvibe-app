/**
 * /forgot-password — request a recovery email. Submitting the email calls the
 * backend (always 204, leak-safe) and the page shows the same "check your inbox"
 * confirmation regardless of whether the account exists.
 */

import ForgotPasswordPage from "@/app/forgot-password/page";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const requestPasswordReset = vi.fn();
vi.mock("@/lib/api/auth", () => ({
  requestPasswordReset: (...args: unknown[]) => requestPasswordReset(...args),
}));

describe("forgot password page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the email form and a back-to-sign-in link", () => {
    render(<ForgotPasswordPage />);
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send reset link" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to sign in" })).toHaveAttribute("href", "/login");
  });

  it("sends the reset request and shows the confirmation", async () => {
    requestPasswordReset.mockResolvedValue(undefined);
    render(<ForgotPasswordPage />);

    await userEvent.type(screen.getByLabelText("Email"), "founder@bsvibe.dev");
    await userEvent.click(screen.getByRole("button", { name: "Send reset link" }));

    await waitFor(() => expect(requestPasswordReset).toHaveBeenCalledWith("founder@bsvibe.dev"));
    expect(await screen.findByText("Check your inbox")).toBeInTheDocument();
  });

  it("still shows the confirmation when the backend errors (leak-safe)", async () => {
    requestPasswordReset.mockRejectedValue(new Error("boom"));
    render(<ForgotPasswordPage />);

    await userEvent.type(screen.getByLabelText("Email"), "ghost@bsvibe.dev");
    await userEvent.click(screen.getByRole("button", { name: "Send reset link" }));

    expect(await screen.findByText("Check your inbox")).toBeInTheDocument();
  });
});
