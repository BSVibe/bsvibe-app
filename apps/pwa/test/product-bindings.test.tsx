/**
 * ProductBindings — per-Product × ConnectorAccount 3-knob binding surface
 * (components/products/ProductBindings.tsx).
 *
 * Listing, the `output_mode` select, Add form, and Remove. All API clients (+ the
 * connector list for the Add dropdown) are injected so the surface is
 * unit-testable against mocks without monkey-patching the module — mirrors
 * ProductResources.
 *
 * Mock fixtures mirror the REAL backend response shape 1:1 (ResourceBinding
 * fields) to avoid e2e-mock-shape-drift.
 *
 * ⚠️ 2026-09-22 — 그 드리프트가 실제로 있었다. #924 가 `trigger.enabled` 를
 * 지운 뒤에도 이 픽스처가 그 키를 **넣어 줬고**, 그래서 없는 값을 읽는 체크박스와
 * REST 가 422 로 거절하는 PATCH 가 11일간 초록이었다. 픽스처는 프로덕션이
 * **주는 것만** 줘야 한다 — 보류하는 것을 채워 주면 테스트가 실명한다.
 */

import ProductBindings from "@/components/products/ProductBindings";
import type {
  Connector,
  ResourceBinding,
  ResourceBindingCreate,
  ResourceBindingUpdate,
} from "@/lib/api/types";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const PRODUCT_ID = "11111111-1111-1111-1111-111111111111";
const CONNECTOR_ID = "33333333-3333-3333-3333-333333333333";

function binding(over: Partial<ResourceBinding> = {}): ResourceBinding {
  return {
    id: "22222222-2222-2222-2222-222222222222",
    workspace_id: "ws-1",
    product_id: PRODUCT_ID,
    connector_account_id: CONNECTOR_ID,
    connector: "telegram",
    resource_id: "acme/blog",
    selection: {},
    trigger: { filters: {} },
    output_mode: "safe",
    created_at: "2026-05-26T00:00:00Z",
    updated_at: "2026-05-26T00:00:00Z",
    ...over,
  };
}

function connector(over: Partial<Connector> = {}): Connector {
  return {
    id: CONNECTOR_ID,
    connector: "github",
    external_ref: "acme",
    is_active: true,
    created_at: "2026-05-26T00:00:00Z",
    delivery_config: {},
    token_hint: "...abcd",
    outbound: true,
    importable: false,
    webhook_trigger: true,
    ...over,
  };
}

describe("ProductBindings", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the bindings heading and lists bindings with the output-mode knob", async () => {
    const listBindings = vi.fn().mockResolvedValue([binding()]);
    render(<ProductBindings productId={PRODUCT_ID} listBindings={listBindings} />);

    const section = await screen.findByRole("region", { name: /Connector bindings/i });
    expect(within(section).getByText("acme/blog")).toBeInTheDocument();
    // The dead knob's control is gone — it read a key production stopped
    // sending and wrote one REST refuses with a 422.
    expect(within(section).queryByRole("checkbox")).toBeNull();
    // Output mode select reflects `safe`.
    const output = within(section).getByRole("combobox", { name: /Output/i });
    expect(output).toHaveValue("safe");
  });

  it("says which connector the row is, and does not show a raw uuid fragment", async () => {
    // #1042, 형님 실사용 피드백(2026-09-23). 화면이 이랬다:
    //
    //     BSVibe/bsvibe-app   be3514f6
    //     8242700007          7bfb9d70
    //
    // `be3514f6` 는 uuid 앞 8자다 — 형님께 아무 의미가 없는데 제목 옆 가장 눈에
    // 띄는 자리에 있었고, `aria-hidden` 이라 **시각 사용자에게만 보이는 노이즈**였다.
    // 정작 `8242700007` 이 텔레그램 채팅이라는 건 화면 어디에도 없었다.
    const listBindings = vi.fn().mockResolvedValue([binding()]);
    render(<ProductBindings productId={PRODUCT_ID} listBindings={listBindings} />);

    const section = await screen.findByRole("region", { name: /Connector bindings/i });
    expect(within(section).getByText(/telegram/i)).toBeInTheDocument();
    // uuid 앞 8자가 사라졌다
    expect(within(section).queryByText(CONNECTOR_ID.slice(0, 8))).toBeNull();
  });

  it("degrades calmly when the server does not say the connector", async () => {
    // 음성 대조군 — 서버가 예전 응답을 주면(필드 없음) 깨지지 않고 조용히 비운다.
    const listBindings = vi.fn().mockResolvedValue([binding({ connector: undefined })]);
    render(<ProductBindings productId={PRODUCT_ID} listBindings={listBindings} />);
    const section = await screen.findByRole("region", { name: /Connector bindings/i });
    expect(within(section).getByText("acme/blog")).toBeInTheDocument();
  });

  it("shows a calm empty state when there are no bindings", async () => {
    const listBindings = vi.fn().mockResolvedValue([]);
    render(<ProductBindings productId={PRODUCT_ID} listBindings={listBindings} />);
    expect(await screen.findByText(/No connector bindings yet/i)).toBeInTheDocument();
  });

  it("degrades to a calm note when the load fails", async () => {
    const listBindings = vi.fn().mockRejectedValue(new Error("boom"));
    render(<ProductBindings productId={PRODUCT_ID} listBindings={listBindings} />);
    expect(await screen.findByText(/Couldn.t load/i)).toBeInTheDocument();
  });

  it("never sends the dead trigger knob in any mutation", async () => {
    // The guard that keeps the 422 from coming back. `output_mode` is the one
    // knob this surface writes; whatever it sends must be a body REST accepts.
    const listBindings = vi.fn().mockResolvedValue([binding()]);
    const updateBinding = vi
      .fn<(id: string, bid: string, p: ResourceBindingUpdate) => Promise<ResourceBinding>>()
      .mockResolvedValue(binding({ output_mode: "direct" }));

    render(
      <ProductBindings
        productId={PRODUCT_ID}
        listBindings={listBindings}
        updateBinding={updateBinding}
      />,
    );

    const select = await screen.findByRole("combobox", { name: /Output/i });
    await userEvent.selectOptions(select, "direct");

    await waitFor(() => expect(updateBinding).toHaveBeenCalled());
    for (const call of updateBinding.mock.calls) {
      expect(JSON.stringify(call[2])).not.toContain("enabled");
    }
  });

  it("changes output_mode via PATCH and re-reads", async () => {
    const listBindings = vi.fn().mockResolvedValue([binding()]);
    const updateBinding = vi
      .fn<(id: string, bid: string, p: ResourceBindingUpdate) => Promise<ResourceBinding>>()
      .mockResolvedValue(binding({ output_mode: "direct" }));

    render(
      <ProductBindings
        productId={PRODUCT_ID}
        listBindings={listBindings}
        updateBinding={updateBinding}
      />,
    );

    const select = await screen.findByRole("combobox", { name: /Output/i });
    await userEvent.selectOptions(select, "direct");

    await waitFor(() =>
      expect(updateBinding).toHaveBeenCalledWith(
        PRODUCT_ID,
        "22222222-2222-2222-2222-222222222222",
        expect.objectContaining({ output_mode: "direct" }),
      ),
    );
  });

  it("surfaces an inline error when a knob change fails — not a silent revert", async () => {
    const listBindings = vi.fn().mockResolvedValue([binding()]);
    const updateBinding = vi
      .fn<(id: string, bid: string, p: ResourceBindingUpdate) => Promise<ResourceBinding>>()
      .mockRejectedValue(new Error("boom"));

    render(
      <ProductBindings
        productId={PRODUCT_ID}
        listBindings={listBindings}
        updateBinding={updateBinding}
      />,
    );

    const select = await screen.findByRole("combobox", { name: /Output/i });
    await userEvent.selectOptions(select, "direct");

    // The failure is visible, not swallowed by the silent re-read.
    expect(await screen.findByText(/couldn.t save that change/i)).toBeInTheDocument();
  });

  it("surfaces an inline error when a remove fails", async () => {
    const listBindings = vi.fn().mockResolvedValue([binding()]);
    const removeBinding = vi.fn().mockRejectedValue(new Error("boom"));

    render(
      <ProductBindings
        productId={PRODUCT_ID}
        listBindings={listBindings}
        removeBinding={removeBinding}
      />,
    );

    await screen.findByText("acme/blog");
    await userEvent.click(screen.getByRole("button", { name: /Remove/i }));

    expect(await screen.findByText(/couldn.t save that change/i)).toBeInTheDocument();
  });

  it("opens the add form, lists connectors, and creates a binding on submit", async () => {
    const listBindings = vi
      .fn()
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([binding({ resource_id: "acme/web" })]);
    const listConnectors = vi.fn().mockResolvedValue([connector()]);
    const createBinding = vi
      .fn<(id: string, input: ResourceBindingCreate) => Promise<ResourceBinding>>()
      .mockResolvedValue(binding({ resource_id: "acme/web" }));

    render(
      <ProductBindings
        productId={PRODUCT_ID}
        listBindings={listBindings}
        listConnectors={listConnectors}
        createBinding={createBinding}
      />,
    );

    await screen.findByText(/No connector bindings yet/i);
    await userEvent.click(screen.getByRole("button", { name: /Add binding/i }));

    // Connector dropdown populates from listConnectors.
    await waitFor(() => expect(listConnectors).toHaveBeenCalled());
    await screen.findByRole("combobox", { name: /Connector$/i });

    await userEvent.type(screen.getByLabelText(/Resource id/i), "acme/web");
    await userEvent.click(screen.getByRole("button", { name: /^Add$/i }));

    await waitFor(() =>
      expect(createBinding).toHaveBeenCalledWith(
        PRODUCT_ID,
        expect.objectContaining({
          connector_account_id: CONNECTOR_ID,
          resource_id: "acme/web",
          output_mode: "safe",
        }),
      ),
    );
    // List re-read after a successful add.
    await waitFor(() => expect(listBindings).toHaveBeenCalledTimes(2));
  });

  it("removes a binding via the Remove affordance and re-reads", async () => {
    const listBindings = vi.fn().mockResolvedValueOnce([binding()]).mockResolvedValueOnce([]);
    const removeBinding = vi.fn().mockResolvedValue(undefined);

    render(
      <ProductBindings
        productId={PRODUCT_ID}
        listBindings={listBindings}
        removeBinding={removeBinding}
      />,
    );

    await screen.findByText("acme/blog");
    await userEvent.click(screen.getByRole("button", { name: /Remove/i }));

    await waitFor(() =>
      expect(removeBinding).toHaveBeenCalledWith(
        PRODUCT_ID,
        "22222222-2222-2222-2222-222222222222",
      ),
    );
    await waitFor(() => expect(listBindings).toHaveBeenCalledTimes(2));
  });
});
