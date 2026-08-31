/**
 * Settings → Notifications (N2b): the real events × channels matrix.
 *
 * Delivery is now wired (N2a/N3 — needs_you/triggered/shipped/failed deliver via
 * the NotifyWorker), so the "coming soon" stub is gone and this surface renders a
 * live grid. These tests pin the honesty invariants:
 *
 *  - Columns are DERIVED from `available_channels` (not a hardcoded list): a
 *    telegram-bound workspace shows a Telegram column and NOT a Slack one.
 *  - Toggling a push cell PUTs the flipped matrix through `updateNotificationPrefs`.
 *  - Zero push connectors (`available_channels == ["in_app"]`) ⇒ a "connect a
 *    channel" empty state with a deep link, never a bare in-app-only grid.
 *  - `daily_brief` is a LIVE toggle: it has a producer (DailyBriefWorker) and the
 *    founder must be able to turn it off.
 *
 * The tab fetches prefs on mount, so every assertion against the rendered grid
 * uses `findBy*` (async, retries) — a sync `getBy*` right after render passes
 * locally and flakes in CI.
 */

import NotificationsTab from "@/components/settings/NotificationsTab";
import type { NotificationPrefsView } from "@/lib/api/types";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
}));

const { getNotificationPrefs, updateNotificationPrefs } = vi.hoisted(() => ({
  getNotificationPrefs: vi.fn(),
  updateNotificationPrefs: vi.fn(),
}));

vi.mock("@/lib/api/notifications", () => ({
  getNotificationPrefs,
  updateNotificationPrefs,
}));

function prefs(overrides: Partial<NotificationPrefsView> = {}): NotificationPrefsView {
  return {
    matrix: {
      needs_you: true,
      triggered: true,
      shipped: true,
      failed: true,
      daily_brief: true,
    },
    quiet_hours_enabled: false,
    quiet_hours_start: "22:00",
    quiet_hours_end: "08:00",
    available_channels: ["in_app", "telegram"],
    ...overrides,
  };
}

beforeEach(() => {
  getNotificationPrefs.mockReset();
  updateNotificationPrefs.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("NotificationsTab — one switch per event", () => {
  it("names where an enabled event will actually land", async () => {
    // 채널 축은 사라졌지만 `available_channels` 는 여전히 쓰인다 — 이제 열이 아니라
    // 캡션으로. 스위치가 "어디로 가는지" 말하지 않으면 과약속이 된다.
    getNotificationPrefs.mockResolvedValue(prefs());
    render(<NotificationsTab />);

    expect(await screen.findByText(/telegram/i)).toBeInTheDocument();
    expect(screen.queryByText(/slack/i)).toBeNull();
  });

  it("toggling an event PUTs the flipped matrix", async () => {
    getNotificationPrefs.mockResolvedValue(prefs());
    updateNotificationPrefs.mockResolvedValue(prefs());
    render(<NotificationsTab />);

    const cell = await screen.findByRole("checkbox", { name: /needs you/i });
    expect(cell).toBeChecked();
    await userEvent.click(cell);

    expect(updateNotificationPrefs).toHaveBeenCalledWith(
      expect.objectContaining({
        matrix: expect.objectContaining({
          needs_you: false,
        }),
      }),
    );
  });

  it("has exactly one switch per event — no per-channel cells", async () => {
    // 채널 축이 살아 있으면 이벤트당 체크박스가 여러 개다. 하나여야 한다.
    getNotificationPrefs.mockResolvedValue(prefs());
    render(<NotificationsTab />);
    await screen.findByRole("checkbox", { name: /needs you/i });
    expect(screen.getAllByRole("checkbox", { name: /needs you/i })).toHaveLength(1);
  });

  // `daily_brief` was rendered inert on the stated grounds that it "has NO
  // producer yet (it needs the Schedule track)". Measured in prod 2026-08-26:
  // `DailyBriefWorker` is running and `notification_events` holds 76 daily_brief
  // rows, ALL `sent`, the most recent that same day — and the founder's own prefs
  // have it enabled on a push channel. The worker even gates on those prefs
  // ("skips the workspace unless daily_brief is enabled for at least one
  // channel"), so the events prove the toggle is ON.
  //
  // So the grid was not merely withholding a switch: it drew an ENABLED
  // preference as an unchecked, disabled box. The founder could not turn off
  // something they were receiving, and the UI told them they were not receiving it.

  it("daily_brief is a live toggle — it has a producer", async () => {
    getNotificationPrefs.mockResolvedValue(prefs());
    render(<NotificationsTab />);
    const cell = await screen.findByRole("checkbox", { name: /daily digest/i });
    expect(cell).toBeEnabled();
  });

  it("daily_brief reflects the stored preference instead of a hardcoded false", async () => {
    getNotificationPrefs.mockResolvedValue(prefs());
    render(<NotificationsTab />);
    const cell = await screen.findByRole("checkbox", { name: /daily digest/i });
    expect(cell).toBeChecked();
  });

  it("turning daily_brief OFF PUTs the flipped matrix", async () => {
    getNotificationPrefs.mockResolvedValue(prefs());
    updateNotificationPrefs.mockResolvedValue(prefs());
    render(<NotificationsTab />);
    const cell = await screen.findByRole("checkbox", { name: /daily digest/i });
    await userEvent.click(cell);

    expect(updateNotificationPrefs).toHaveBeenCalledTimes(1);
    const sent = updateNotificationPrefs.mock.calls[0][0];
    expect(sent.matrix.daily_brief).toBe(false);
  });

  it("zero push connectors ⇒ connect-a-channel empty state, no grid", async () => {
    getNotificationPrefs.mockResolvedValue(prefs({ available_channels: ["in_app"] }));
    render(<NotificationsTab />);

    expect(await screen.findByRole("link", { name: /connect/i })).toHaveAttribute(
      "href",
      "/settings/connectors",
    );
    // No events × channels grid when there is nothing to push to.
    expect(screen.queryByRole("columnheader", { name: /telegram/i })).toBeNull();
  });

  it("quiet-hours enable toggle PUTs the flag", async () => {
    getNotificationPrefs.mockResolvedValue(prefs());
    updateNotificationPrefs.mockResolvedValue(prefs({ quiet_hours_enabled: true }));
    render(<NotificationsTab />);

    const toggle = await screen.findByRole("checkbox", { name: /quiet hours/i });
    await userEvent.click(toggle);
    expect(updateNotificationPrefs).toHaveBeenCalledWith(
      expect.objectContaining({ quiet_hours_enabled: true }),
    );
  });
});
