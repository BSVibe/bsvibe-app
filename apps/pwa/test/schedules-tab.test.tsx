/**
 * Settings → Schedules (S3): author the instructions BSVibe runs on its own.
 *
 * The surface is the PWA producer for `workspace_schedules` (the channel S1's
 * REST endpoints write to). These tests pin the honesty + wiring invariants:
 *
 *  - Only the `instruction` kind is offered — there is NO kind selector pushing
 *    skill / product_tick / plugin_action (those are S4, not built).
 *  - A cron PRESET button fills the cron field with the right expression.
 *  - Submitting the create form calls `createSchedule` with the instruction text
 *    and the chosen cron (kind: "instruction").
 *  - Toggling enable calls `setScheduleEnabled`; the delete control calls
 *    `deleteSchedule`.
 *  - Empty state when there are no schedules.
 *
 * The tab fetches schedules (and the workspace tz) on mount, so every assertion
 * against the rendered list/form uses `findBy*` (async, retries) — a sync
 * `getBy*` right after render passes locally and flakes in CI.
 */

import SchedulesTab from "@/components/settings/SchedulesTab";
import type { Schedule } from "@/lib/api/schedules";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
}));

const { getSchedules, createSchedule, deleteSchedule, setScheduleEnabled } = vi.hoisted(() => ({
  getSchedules: vi.fn(),
  createSchedule: vi.fn(),
  deleteSchedule: vi.fn(),
  setScheduleEnabled: vi.fn(),
}));

vi.mock("@/lib/api/schedules", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/schedules")>();
  return {
    ...actual,
    getSchedules,
    createSchedule,
    deleteSchedule,
    setScheduleEnabled,
  };
});

const { getWorkspace } = vi.hoisted(() => ({ getWorkspace: vi.fn() }));
vi.mock("@/lib/api/workspace", () => ({ getWorkspace }));

function schedule(overrides: Partial<Schedule> = {}): Schedule {
  return {
    id: "sched-1",
    kind: "instruction",
    text: "매주 월요일 시장조사 요약해줘",
    cron_expr: "0 9 * * 1",
    product_id: null,
    title: "주간 시장조사",
    next_run_at: "2026-07-20T00:00:00Z",
    last_fired_at: null,
    enabled: true,
    ...overrides,
  };
}

beforeEach(() => {
  getSchedules.mockReset();
  createSchedule.mockReset();
  deleteSchedule.mockReset();
  setScheduleEnabled.mockReset();
  getWorkspace.mockReset();
  getWorkspace.mockResolvedValue({ id: "ws-1", name: "BSVibe", timezone: "Asia/Seoul" });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SchedulesTab — authoring surface", () => {
  it("renders an existing schedule after the on-mount fetch (findBy*)", async () => {
    getSchedules.mockResolvedValue([schedule()]);
    render(<SchedulesTab />);

    expect(await screen.findByText("주간 시장조사")).toBeInTheDocument();
    expect(await screen.findByText(/매주 월요일 시장조사 요약해줘/)).toBeInTheDocument();
  });

  it("shows the empty state when there are no schedules", async () => {
    getSchedules.mockResolvedValue([]);
    render(<SchedulesTab />);

    expect(await screen.findByText(/no scheduled/i)).toBeInTheDocument();
  });

  it("offers NO kind selector (only the instruction kind is honest)", async () => {
    getSchedules.mockResolvedValue([]);
    render(<SchedulesTab />);
    // Wait for the form to settle.
    await screen.findByRole("textbox", { name: /instruction/i });
    // No skill / product_tick / plugin_action option anywhere.
    expect(screen.queryByText(/product_tick/i)).toBeNull();
    expect(screen.queryByText(/plugin_action/i)).toBeNull();
    expect(screen.queryByRole("combobox", { name: /kind/i })).toBeNull();
  });

  it("a cron preset button fills the cron field with the right expression", async () => {
    getSchedules.mockResolvedValue([]);
    render(<SchedulesTab />);

    const cronField = (await screen.findByRole("textbox", {
      name: /cron/i,
    })) as HTMLInputElement;
    await userEvent.click(await screen.findByRole("button", { name: /every hour/i }));
    expect(cronField.value).toBe("0 * * * *");
  });

  it("submitting the create form calls createSchedule with instruction + cron", async () => {
    getSchedules.mockResolvedValue([]);
    createSchedule.mockResolvedValue(schedule());
    render(<SchedulesTab />);

    const instruction = await screen.findByRole("textbox", { name: /instruction/i });
    await userEvent.type(instruction, "매주 월요일 시장조사 요약해줘");
    await userEvent.click(await screen.findByRole("button", { name: /every monday/i }));
    await userEvent.click(screen.getByRole("button", { name: /add schedule/i }));

    expect(createSchedule).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "instruction",
        text: "매주 월요일 시장조사 요약해줘",
        cron_expr: "0 9 * * 1",
      }),
    );
  });

  it("toggling enable calls setScheduleEnabled", async () => {
    getSchedules.mockResolvedValue([schedule({ enabled: true })]);
    setScheduleEnabled.mockResolvedValue(schedule({ enabled: false }));
    render(<SchedulesTab />);

    const toggle = await screen.findByRole("checkbox", { name: /enabled/i });
    expect(toggle).toBeChecked();
    await userEvent.click(toggle);

    expect(setScheduleEnabled).toHaveBeenCalledWith("sched-1", false);
  });

  it("delete calls deleteSchedule", async () => {
    getSchedules.mockResolvedValue([schedule()]);
    deleteSchedule.mockResolvedValue(undefined);
    render(<SchedulesTab />);

    const del = await screen.findByRole("button", { name: /delete/i });
    await userEvent.click(del);

    expect(deleteSchedule).toHaveBeenCalledWith("sched-1");
  });
});
