/**
 * ProductBindings — per-Product × ConnectorAccount 3-knob binding surface
 * (components/products/ProductBindings.tsx).
 *
 * Listing, the two minimal knob controls (`trigger.enabled` checkbox +
 * `output_mode` select), Add form, and Remove. All API clients (+ the
 * connector list for the Add dropdown) are injected so the surface is
 * unit-testable against mocks without monkey-patching the module — mirrors
 * ProductResources.
 *
 * Mock fixtures mirror the REAL backend response shape 1:1 (ResourceBinding
 * fields) to avoid e2e-mock-shape-drift.
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
    resource_id: "acme/blog",
    selection: {},
    trigger: { enabled: false, filters: {} },
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
    ...over,
  };
}

describe("ProductBindings", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the bindings heading and lists bindings with the two knob controls", async () => {
    const listBindings = vi.fn().mockResolvedValue([binding()]);
    render(<ProductBindings productId={PRODUCT_ID} listBindings={listBindings} />);

    const section = await screen.findByRole("region", { name: /Connector bindings/i });
    expect(within(section).getByText("acme/blog")).toBeInTheDocument();
    // Trigger checkbox renders + reflects the default `enabled=false`.
    const trigger = within(section).getByRole("checkbox", { name: /Trigger on/i });
    expect(trigger).not.toBeChecked();
    // Output mode select reflects `safe`.
    const output = within(section).getByRole("combobox", { name: /Output/i });
    expect(output).toHaveValue("safe");
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

  it("toggles trigger.enabled via PATCH and re-reads", async () => {
    const listBindings = vi
      .fn()
      .mockResolvedValueOnce([binding({ trigger: { enabled: false, filters: {} } })])
      .mockResolvedValueOnce([binding({ trigger: { enabled: true, filters: {} } })]);
    const updateBinding = vi
      .fn<(id: string, bid: string, p: ResourceBindingUpdate) => Promise<ResourceBinding>>()
      .mockResolvedValue(binding({ trigger: { enabled: true, filters: {} } }));

    render(
      <ProductBindings
        productId={PRODUCT_ID}
        listBindings={listBindings}
        updateBinding={updateBinding}
      />,
    );

    const checkbox = await screen.findByRole("checkbox", { name: /Trigger on/i });
    await userEvent.click(checkbox);

    await waitFor(() =>
      expect(updateBinding).toHaveBeenCalledWith(
        PRODUCT_ID,
        "22222222-2222-2222-2222-222222222222",
        expect.objectContaining({
          trigger: expect.objectContaining({ enabled: true }),
        }),
      ),
    );
    // Re-read fired.
    await waitFor(() => expect(listBindings).toHaveBeenCalledTimes(2));
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
