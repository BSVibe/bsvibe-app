/**
 * #961 — the founder can author a `product_tick` schedule from the PWA.
 *
 * A tick is "for THIS product, BSVibe decides and does the next step" on a cadence. It takes
 * no instruction text and REQUIRES a product, and it always waits for approval (PT3: BSVibe
 * chose the work itself, so it can never be "deliver without approval"). Product decisions
 * (founder, 2026-10-19 — "961부터 하자", defaults filled in here):
 *
 *  - one product → it is pre-selected; several → none is, the founder picks (a wrong default
 *    would start autonomous work on the wrong product);
 *  - no products → the tick option is shown but disabled, saying why;
 *  - deleting a product retires its ticks (backend, same PR).
 */
import SchedulesTab from "@/components/settings/SchedulesTab";
import type { Schedule } from "@/lib/api/schedules";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
}));

const { getSchedules, createSchedule, listProducts, getWorkspace } = vi.hoisted(() => ({
  getSchedules: vi.fn(),
  createSchedule: vi.fn(),
  listProducts: vi.fn(),
  getWorkspace: vi.fn(),
}));
vi.mock("@/lib/api/schedules", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api/schedules")>()),
  getSchedules,
  createSchedule,
  deleteSchedule: vi.fn(),
  setScheduleEnabled: vi.fn(),
}));
vi.mock("@/lib/api/products", () => ({ listProducts }));
vi.mock("@/lib/api/workspace", () => ({ getWorkspace }));

function product(id: string, name: string) {
  return { id, name, slug: name.toLowerCase(), workspace_id: "ws" };
}

function tickRow(over: Partial<Schedule> = {}): Schedule {
  return {
    id: "t1",
    kind: "product_tick",
    text: "",
    cron_expr: "0 9 * * *",
    product_id: "p1",
    title: null,
    next_run_at: "2026-10-20T00:00:00Z",
    last_fired_at: null,
    enabled: true,
    output_mode: "safe",
    ...over,
  };
}

beforeEach(() => {
  getSchedules.mockReset().mockResolvedValue([]);
  createSchedule.mockReset().mockImplementation(async (body) => tickRow(body));
  listProducts.mockReset().mockResolvedValue([product("p1", "BStockReport")]);
  getWorkspace.mockResolvedValue({ id: "ws", name: "W", timezone: "Asia/Seoul" });
});

async function chooseTick() {
  const user = userEvent.setup();
  render(<SchedulesTab />);
  await user.click(await screen.findByRole("radio", { name: /product check/i }));
  return user;
}

describe("product_tick authoring", () => {
  it("offers exactly the two kinds the backend runs — never skill / plugin_action", async () => {
    render(<SchedulesTab />);
    const radios = await screen.findAllByRole("radio");
    expect(radios.map((r) => r.getAttribute("value"))).toEqual(["instruction", "product_tick"]);
    expect(screen.queryByText(/plugin_action|\bskill\b/i)).toBeNull();
  });

  it("a tick takes a product instead of an instruction, and never 'deliver without approval'", async () => {
    await chooseTick();

    expect(screen.queryByRole("textbox", { name: /instruction/i })).toBeNull();
    expect(screen.getByRole("combobox", { name: /product/i })).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /without approval/i })).toBeNull();
    expect(screen.getByText(/always wait for your approval/i)).toBeInTheDocument();
  });

  it("with one product it is chosen, and the tick is created for it", async () => {
    const user = await chooseTick();
    await user.click(screen.getByRole("button", { name: /every day at 09:00/i }));
    await user.click(screen.getByRole("button", { name: /add schedule/i }));

    expect(createSchedule).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "product_tick",
        product_id: "p1",
        cron_expr: "0 9 * * *",
        output_mode: "safe",
      }),
    );
  });

  it("'deliver without approval' set on an instruction does not carry over to a tick", async () => {
    const user = userEvent.setup();
    render(<SchedulesTab />);
    await user.click(await screen.findByRole("checkbox", { name: /without approval/i }));
    await user.click(screen.getByRole("radio", { name: /product check/i }));
    await user.click(screen.getByRole("button", { name: /every day at 09:00/i }));
    await user.click(screen.getByRole("button", { name: /add schedule/i }));

    expect(createSchedule).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "product_tick", output_mode: "safe" }),
    );
  });

  it("with several products none is pre-chosen — submit waits for the founder", async () => {
    listProducts.mockResolvedValue([product("p1", "BStockReport"), product("p2", "BSVibe")]);
    const user = await chooseTick();
    await user.click(screen.getByRole("button", { name: /every day at 09:00/i }));

    const submit = screen.getByRole("button", { name: /add schedule/i });
    expect(submit).toBeDisabled();
    await user.selectOptions(screen.getByRole("combobox", { name: /product/i }), "p2");
    expect(submit).toBeEnabled();
  });

  it("with no products the tick option is disabled and says why", async () => {
    listProducts.mockResolvedValue([]);
    render(<SchedulesTab />);

    const tick = await screen.findByRole("radio", { name: /product check/i });
    expect(tick).toBeDisabled();
    expect(screen.getByText(/needs a product/i)).toBeInTheDocument();
  });

  it("a tick in the list is named by its product, not left blank", async () => {
    getSchedules.mockResolvedValue([tickRow()]);
    render(<SchedulesTab />);

    expect(await screen.findByText(/product check · BStockReport/i)).toBeInTheDocument();
  });
});
