/**
 * Direct compose submit flow — opens the overlay, types, submits, and asserts
 * it POSTs /api/v1/messages and shows the success / error states. fetch mocked.
 */

import { DIRECT_SUBMITTED_EVENT, DirectOverlay } from "@/components/shell/DirectAction";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const ACCEPTED = { accepted: true, duplicate: false, workspace_id: "ws-1" };

describe("Direct compose", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("does not render when closed", () => {
    render(<DirectOverlay open={false} onClose={() => {}} />);
    expect(screen.queryByRole("dialog", { name: "Direct" })).not.toBeInTheDocument();
  });

  it("disables submit until the textarea has content", async () => {
    render(<DirectOverlay open onClose={() => {}} />);
    const submit = screen.getByRole("button", { name: "Direct" });
    expect(submit).toBeDisabled();

    await userEvent.type(screen.getByRole("textbox"), "draft the launch post");
    expect(submit).toBeEnabled();
  });

  it("POSTs /api/v1/messages, shows success, emits the refresh event", async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify(ACCEPTED), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    const onSubmitted = vi.fn();
    window.addEventListener(DIRECT_SUBMITTED_EVENT, onSubmitted);

    render(<DirectOverlay open onClose={() => {}} />);
    await userEvent.type(screen.getByRole("textbox"), "draft the launch post");
    fireEvent.click(screen.getByRole("button", { name: "Direct" }));

    await waitFor(() => {
      expect(screen.getByText("Sent. Working on it.")).toBeInTheDocument();
    });

    // Hit the real endpoint with a JSON body carrying the typed text.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/messages");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ text: "draft the launch post" });

    expect(onSubmitted).toHaveBeenCalledTimes(1);
    window.removeEventListener(DIRECT_SUBMITTED_EVENT, onSubmitted);
  });

  it("shows an error state when the submit fails", async () => {
    global.fetch = vi.fn(
      async () => new Response("boom", { status: 500 }),
    ) as unknown as typeof fetch;

    render(<DirectOverlay open onClose={() => {}} />);
    await userEvent.type(screen.getByRole("textbox"), "do the thing");
    fireEvent.click(screen.getByRole("button", { name: "Direct" }));

    await waitFor(() => {
      expect(screen.getByText("Couldn’t send that. Please try again.")).toBeInTheDocument();
    });
  });
});
