/**
 * #1072 — a schedule can deliver without an approval card.
 *
 * The founder's weekly report queued for Safe Mode approval every week: the
 * workspace flag always won. A schedule now carries `output_mode` — "safe"
 * (default, follows Safe Mode) or "direct" (delivered without an approval card;
 * the founder approved it by writing the schedule). `product_tick` — BSVibe
 * deciding the work itself — is always "safe", so it offers no such switch.
 */

import SchedulesTab from "@/components/settings/SchedulesTab";
import type { Schedule } from "@/lib/api/schedules";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
}));

const { getSchedules, createSchedule, deleteSchedule, setScheduleEnabled, setScheduleOutputMode } =
  vi.hoisted(() => ({
    getSchedules: vi.fn(),
    createSchedule: vi.fn(),
    deleteSchedule: vi.fn(),
    setScheduleEnabled: vi.fn(),
    setScheduleOutputMode: vi.fn(),
  }));

vi.mock("@/lib/api/schedules", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/schedules")>();
  return {
    ...actual,
    getSchedules,
    createSchedule,
    deleteSchedule,
    setScheduleEnabled,
    setScheduleOutputMode,
  };
});

const { getWorkspace } = vi.hoisted(() => ({ getWorkspace: vi.fn() }));
vi.mock("@/lib/api/workspace", () => ({ getWorkspace }));

function schedule(overrides: Partial<Schedule> = {}): Schedule {
  return {
    id: "sched-1",
    kind: "instruction",
    text: "주간 리포트를 발행해줘",
    cron_expr: "30 0 * * 1",
    product_id: null,
    title: "주간 리포트",
    next_run_at: "2026-10-12T00:30:00Z",
    last_fired_at: null,
    enabled: true,
    output_mode: "safe",
    ...overrides,
  };
}

beforeEach(() => {
  for (const fn of [
    getSchedules,
    createSchedule,
    deleteSchedule,
    setScheduleEnabled,
    setScheduleOutputMode,
  ]) {
    fn.mockReset();
  }
  getWorkspace.mockResolvedValue({ id: "ws-1", name: "BSVibe", timezone: "Asia/Seoul" });
});

describe("SchedulesTab — deliver without approval (#1072)", () => {
  it("an instruction schedule offers the switch, reflecting its mode", async () => {
    getSchedules.mockResolvedValue([schedule({ output_mode: "direct" })]);
    render(<SchedulesTab />);

    const toggle = await screen.findByRole("checkbox", {
      name: /주간 리포트 — deliver without approval/i,
    });
    expect(toggle).toBeChecked();
  });

  it("flipping it calls setScheduleOutputMode", async () => {
    getSchedules.mockResolvedValue([schedule({ output_mode: "safe" })]);
    setScheduleOutputMode.mockResolvedValue(schedule({ output_mode: "direct" }));
    render(<SchedulesTab />);

    await userEvent.click(
      await screen.findByRole("checkbox", { name: /주간 리포트 — deliver without approval/i }),
    );

    expect(setScheduleOutputMode).toHaveBeenCalledWith("sched-1", "direct");
  });

  it("a product_tick schedule offers no such switch", async () => {
    getSchedules.mockResolvedValue([
      schedule({ kind: "product_tick", text: "", product_id: "p-1", title: "자율 틱" }),
    ]);
    render(<SchedulesTab />);

    await screen.findByText("자율 틱");
    expect(
      screen.queryByRole("checkbox", { name: /자율 틱 — deliver without approval/i }),
    ).toBeNull();
  });

  it("a new schedule can be created direct", async () => {
    getSchedules.mockResolvedValue([]);
    createSchedule.mockResolvedValue(schedule({ output_mode: "direct" }));
    render(<SchedulesTab />);

    await userEvent.type(
      await screen.findByRole("textbox", { name: /instruction/i }),
      "주간 리포트를 발행해줘",
    );
    await userEvent.click(await screen.findByRole("button", { name: /every monday/i }));
    await userEvent.click(screen.getByRole("checkbox", { name: /^deliver without approval$/i }));
    await userEvent.click(screen.getByRole("button", { name: /add schedule/i }));

    expect(createSchedule).toHaveBeenCalledWith(expect.objectContaining({ output_mode: "direct" }));
  });
});
