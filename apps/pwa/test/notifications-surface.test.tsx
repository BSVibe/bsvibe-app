/**
 * Notifications surface — the Settings → Notifications tab. Drives the real
 * get/update clients against a mocked fetch and asserts:
 *
 *  - the events × channels matrix renders all 5 events × 3 channels (15 toggles)
 *  - toggling a cell PUTs the changed value (optimistic)
 *  - the quiet-hours on/off + From/Until times persist via a PUT
 *  - a load error shows a calm inline note instead of crashing the surface
 */

import NotificationsTab from "@/components/settings/NotificationsTab";
import type { NotificationPrefs } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const PREFS: NotificationPrefs = {
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

function jsonResponse(body: unknown, status = 200) {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("Notifications surface", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders all 5 events × 3 channels as labelled toggles", async () => {
    global.fetch = vi.fn(async () => jsonResponse(PREFS)) as unknown as typeof fetch;

    render(<NotificationsTab />);

    // The matrix loads.
    await waitFor(() => {
      expect(screen.getByText(/a decision is waiting/i)).toBeInTheDocument();
    });

    // 5 events x 3 channels = 15 cell toggles.
    const cells = screen.getAllByRole("checkbox", { name: /^toggle / });
    expect(cells).toHaveLength(15);

    // Every event row label is present.
    expect(screen.getByText(/work woke up from outside/i)).toBeInTheDocument();
    expect(screen.getByText(/a deliverable was verified/i)).toBeInTheDocument();
    expect(screen.getByText(/verification failed/i)).toBeInTheDocument();
    expect(screen.getByText(/summary every morning/i)).toBeInTheDocument();

    // The three channel column headers.
    expect(screen.getByRole("columnheader", { name: /^in-app$/i })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /^email$/i })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /^slack$/i })).toBeInTheDocument();
  });

  it("toggling a cell PUTs the changed value", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(PREFS)) // initial GET
      // PUT echoes the persisted prefs (the real backend returns the saved row).
      .mockImplementationOnce(async (_url: string, init: RequestInit) =>
        jsonResponse(JSON.parse(init.body as string)),
      );
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<NotificationsTab />);
    await waitFor(() => expect(screen.getByText(/a decision is waiting/i)).toBeInTheDocument());

    // daily_brief / slack starts OFF — flip it ON.
    const cell = screen.getByRole("checkbox", { name: /toggle slack for daily brief/i });
    expect(cell).not.toBeChecked();
    await userEvent.click(cell);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const putCall = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
    expect(putCall[0]).toBe("/api/v1/notifications/prefs");
    expect(putCall[1].method).toBe("PUT");
    const body = JSON.parse(putCall[1].body as string) as NotificationPrefs;
    expect(body.matrix.daily_brief.slack).toBe(true);
    // Optimistic: the cell shows checked immediately.
    expect(cell).toBeChecked();
  });

  it("persists the quiet-hours on/off and the From/Until times", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(PREFS)) // initial GET
      // Every PUT echoes the persisted prefs (the real backend returns the row).
      .mockImplementation(async (_url: string, init: RequestInit) =>
        jsonResponse(JSON.parse(init.body as string)),
      );
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<NotificationsTab />);
    await waitFor(() => expect(screen.getByText(/a decision is waiting/i)).toBeInTheDocument());

    const quiet = screen.getByRole("checkbox", { name: /quiet hours/i });
    expect(quiet).not.toBeChecked();
    await userEvent.click(quiet);

    await waitFor(() => {
      const calls = fetchMock.mock.calls.filter((c) => (c[1] as RequestInit)?.method === "PUT");
      expect(calls.length).toBeGreaterThanOrEqual(1);
      const body = JSON.parse((calls[0][1] as RequestInit).body as string) as NotificationPrefs;
      expect(body.quiet_hours_enabled).toBe(true);
    });

    // Change the From time → another PUT carries the new start.
    const from = screen.getByLabelText(/from/i);
    fireEvent.change(from, { target: { value: "23:30" } });
    await waitFor(() => {
      const putCalls = fetchMock.mock.calls.filter((c) => (c[1] as RequestInit)?.method === "PUT");
      const last = putCalls[putCalls.length - 1];
      const body = JSON.parse((last[1] as RequestInit).body as string) as NotificationPrefs;
      expect(body.quiet_hours_start).toBe("23:30");
    });
  });

  it("shows a calm inline note when the prefs load fails", async () => {
    global.fetch = vi.fn(
      async () => new Response("boom", { status: 500 }),
    ) as unknown as typeof fetch;

    render(<NotificationsTab />);

    await waitFor(() => {
      expect(screen.getByText(/couldn.t load your notification settings/i)).toBeInTheDocument();
    });
  });

  it("keeps the existing tab heading/lede pattern", async () => {
    global.fetch = vi.fn(async () => jsonResponse(PREFS)) as unknown as typeof fetch;

    const { container } = render(<NotificationsTab />);
    await waitFor(() => expect(screen.getByText(/a decision is waiting/i)).toBeInTheDocument());

    // The general-tab lede pattern the stub used.
    const lede = container.querySelector(".general-tab__lede");
    expect(lede).not.toBeNull();
  });

  it("optimistically reverts a cell when its PUT fails", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(PREFS)) // initial GET
      .mockResolvedValueOnce(jsonResponse("bad", 500)); // PUT fails
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<NotificationsTab />);
    await waitFor(() => expect(screen.getByText(/a decision is waiting/i)).toBeInTheDocument());

    const cell = screen.getByRole("checkbox", { name: /toggle slack for daily brief/i });
    await userEvent.click(cell);

    // After the failed PUT, the optimistic flip is rolled back + a calm note shows.
    await waitFor(() => expect(cell).not.toBeChecked());
    const note = within(document.body).getByText(/couldn.t save/i);
    expect(note).toBeInTheDocument();
  });
});
