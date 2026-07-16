/**
 * Notifications surface — the Settings → Notifications tab.
 *
 * Delivery is NOT wired: backend/notifications/ stores preferences only (no
 * sender, channel adapter, dispatcher, or scheduler) and the in-app bell is
 * disabled. So this tab must be HONEST — it shows a "coming soon" state and
 * must NOT present an active events × channels matrix, quiet-hours window, or
 * copy promising emails / morning digests / "in-app always works". It also must
 * not fetch or write the (dormant) prefs, since nothing consumes them.
 */

import NotificationsTab from "@/components/settings/NotificationsTab";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

describe("Notifications surface (honest coming-soon state)", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows a coming-soon state instead of an active delivery surface", () => {
    render(<NotificationsTab />);

    // A clear "coming soon" / not-active signal is present.
    expect(screen.getByText(/coming soon/i)).toBeInTheDocument();
    expect(screen.getByText(/aren.t active yet/i)).toBeInTheDocument();
    // The honest body states plainly that nothing is sent.
    expect(screen.getByText(/nothing is sent/i)).toBeInTheDocument();
  });

  it("renders NO events × channels matrix toggles (nothing to promise)", () => {
    render(<NotificationsTab />);
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("does NOT render the false delivery-promise copy", () => {
    render(<NotificationsTab />);
    // "pings you", "summary every morning", "In-app always works" all implied
    // a delivery the product cannot perform — they must be gone.
    expect(screen.queryByText(/pings you/i)).toBeNull();
    expect(screen.queryByText(/every morning/i)).toBeNull();
    expect(screen.queryByText(/in-app always works/i)).toBeNull();
  });

  it("renders NO active quiet-hours control", () => {
    render(<NotificationsTab />);
    // The From/Until time inputs of the old quiet-hours window are gone.
    expect(document.querySelectorAll('input[type="time"]')).toHaveLength(0);
    expect(screen.queryByLabelText(/^from$/i)).toBeNull();
    expect(screen.queryByLabelText(/^until$/i)).toBeNull();
  });

  it("does NOT fetch or write the dormant prefs", () => {
    const fetchMock = vi.fn();
    global.fetch = fetchMock as unknown as typeof fetch;
    render(<NotificationsTab />);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("keeps the settings tab heading/lede pattern", () => {
    const { container } = render(<NotificationsTab />);
    expect(container.querySelector(".general-tab__lede")).not.toBeNull();
  });
});
