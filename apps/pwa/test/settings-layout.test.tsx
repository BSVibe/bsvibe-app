/**
 * Settings → 알림 / 예약 layout (2026-10-19, founder screenshots; reproduced on the live stack
 * by `e2e-live/settings-layout.spec.ts`).
 *
 * jsdom has no layout, so this pins the STRUCTURE and the stylesheet rules that produce it:
 *
 *  - `.settings-field__caption` is a caption for a label | value ROW (right-aligned, 260px). Used
 *    as a standalone paragraph it became a narrow right-hand column of text. Standalone hints
 *    are `.settings-hint`; a caption only ever lives inside a `.settings-field`.
 *  - the notification events list had no CSS at all → browser bullets.
 *  - the schedule form's inputs sat at the far right of their rows → stacked fields.
 *  - a long schedule instruction was one wall of text → clamped.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import NotificationsTab from "@/components/settings/NotificationsTab";
import SchedulesTab from "@/components/settings/SchedulesTab";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
}));

const { getNotificationPrefs, getSchedules, getWorkspace } = vi.hoisted(() => ({
  getNotificationPrefs: vi.fn(),
  getSchedules: vi.fn(),
  getWorkspace: vi.fn(),
}));
vi.mock("@/lib/api/notifications", () => ({
  getNotificationPrefs,
  updateNotificationPrefs: vi.fn(),
}));
vi.mock("@/lib/api/schedules", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api/schedules")>()),
  getSchedules,
  createSchedule: vi.fn(),
  deleteSchedule: vi.fn(),
  setScheduleEnabled: vi.fn(),
}));
vi.mock("@/lib/api/workspace", () => ({ getWorkspace }));
vi.mock("@/lib/api/products", () => ({ listProducts: () => Promise.resolve([]) }));

// Comments stripped: a rule's selector text would otherwise carry the comment above it.
const css = readFileSync(join(__dirname, "..", "app", "globals.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);

/** Every declaration block whose selector list names ``selector`` — a selector can be styled
 *  in more than one rule, and the one that matters need not be the first. */
function rule(selector: string): string {
  const blocks = [...css.matchAll(/([^{}]+)\{([^}]*)\}/g)].filter(([, sel]) =>
    sel.split(",").some((s) => s.trim() === selector),
  );
  if (blocks.length === 0) throw new Error(`no rule for ${selector}`);
  return blocks.map(([, , body]) => body).join("\n");
}

function captionsOutsideRows(container: HTMLElement): number {
  return [...container.querySelectorAll(".settings-field__caption")].filter(
    (el) => !el.closest(".settings-field"),
  ).length;
}

beforeEach(() => {
  getNotificationPrefs.mockResolvedValue({
    matrix: { needs_you: true, review_ready: true, triggered: true, shipped: true, failed: true },
    quiet_hours_enabled: false,
    quiet_hours_start: "22:00",
    quiet_hours_end: "08:00",
    available_channels: ["in_app", "telegram"],
  });
  getSchedules.mockResolvedValue([
    {
      id: "s1",
      kind: "instruction",
      text: "주간 통합 리포트를 딜리버러블로 발행해줘. ".repeat(20),
      cron_expr: "30 0 * * 1",
      product_id: null,
      title: "BStockReport 주간 리포트",
      next_run_at: "2026-10-26T00:30:00Z",
      last_fired_at: null,
      enabled: true,
      output_mode: "direct",
    },
  ]);
  getWorkspace.mockResolvedValue({ id: "ws-1", name: "BSVibe", timezone: "Asia/Seoul" });
});

describe("settings layout", () => {
  it("알림 — 안내문은 행 캡션이 아니라 독립 안내문이다", async () => {
    const { container } = render(<NotificationsTab />);
    await screen.findByRole("checkbox", { name: /Needs you/i });

    expect(captionsOutsideRows(container)).toBe(0);
    expect(container.querySelectorAll(".settings-hint").length).toBeGreaterThanOrEqual(3);
  });

  it("예약 — 폼 필드는 쌓이고, 안내문은 독립 안내문이다", async () => {
    const { container } = render(<SchedulesTab />);
    await screen.findByText("BStockReport 주간 리포트");

    expect(captionsOutsideRows(container)).toBe(0);
    const form = container.querySelector(".schedules-form");
    expect(form?.querySelectorAll(".settings-field:not(.settings-field--stacked)")).toHaveLength(0);
    // The full instruction is still there to read on hover, even when clamped.
    const text = container.querySelector(".schedule-row__text");
    expect(text?.getAttribute("title")).toContain("주간 통합 리포트");
  });

  it("스타일시트 — 그 구조를 그리는 규칙들", () => {
    expect(rule(".settings-hint")).not.toMatch(/text-align:\s*right/);
    expect(rule(".notifications-events")).toMatch(/list-style:\s*none/);
    expect(rule(".settings-field--stacked")).toMatch(/flex-direction:\s*column/);
    expect(rule(".settings-field--stacked .settings-field__input")).toMatch(/width:\s*100%/);
    expect(rule(".schedule-row__main")).toMatch(/min-width:\s*0/);
    expect(rule(".schedule-row__text")).toMatch(/-webkit-line-clamp:\s*3/);
  });
});
