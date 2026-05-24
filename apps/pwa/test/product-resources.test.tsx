/**
 * Product RESOURCES section + Add resource modal
 * (components/products/ProductResources.tsx + AddResourceForm.tsx).
 *
 * The section lists a product's resources (title + kind chip + url link +
 * remove affordance), offers an "Add resource" button that opens a native
 * <dialog> hosting the add form, and re-reads the list after a successful add.
 * A failed read degrades to a calm inline note rather than crashing.
 *
 * Both the list/mutate clients are injected so the surface is unit-testable
 * against mocks without monkey-patching the module (mirrors NewProductForm).
 */

import AddResourceForm from "@/components/products/AddResourceForm";
import ProductResources from "@/components/products/ProductResources";
import type { ProductResource, ProductResourceCreate } from "@/lib/api/types";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const PRODUCT_ID = "11111111-1111-1111-1111-111111111111";

function resource(over: Partial<ProductResource> = {}): ProductResource {
  return {
    id: "22222222-2222-2222-2222-222222222222",
    product_id: PRODUCT_ID,
    workspace_id: "ws-1",
    kind: "repo",
    title: "Main repo",
    url: "https://github.com/acme/blog",
    note: null,
    created_at: "2026-05-25T00:00:00Z",
    ...over,
  };
}

describe("Product RESOURCES section", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders a RESOURCES heading and lists resources with a kind chip + link", async () => {
    const listResources = vi.fn().mockResolvedValue([resource()]);
    render(<ProductResources productId={PRODUCT_ID} listResources={listResources} />);

    const section = await screen.findByRole("region", { name: /resources/i });
    expect(within(section).getByText("Main repo")).toBeInTheDocument();
    // The kind chip renders the exact kind value ("repo").
    expect(within(section).getByText("repo")).toBeInTheDocument();
    const link = within(section).getByRole("link", { name: /open/i });
    expect(link).toHaveAttribute("href", "https://github.com/acme/blog");
  });

  it("shows a calm empty state when there are no resources", async () => {
    const listResources = vi.fn().mockResolvedValue([]);
    render(<ProductResources productId={PRODUCT_ID} listResources={listResources} />);

    expect(await screen.findByText(/No resources yet/i)).toBeInTheDocument();
  });

  it("degrades to a calm note when the load fails (no crash)", async () => {
    const listResources = vi.fn().mockRejectedValue(new Error("boom"));
    render(<ProductResources productId={PRODUCT_ID} listResources={listResources} />);

    expect(await screen.findByText(/Couldn.t load/i)).toBeInTheDocument();
  });

  it("opens the Add resource modal and re-reads the list after a successful add", async () => {
    const listResources = vi
      .fn()
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([resource({ title: "New doc", kind: "doc", url: null })]);
    const addResource = vi
      .fn<(id: string, input: ProductResourceCreate) => Promise<ProductResource>>()
      .mockResolvedValue(resource({ title: "New doc", kind: "doc", url: null }));

    render(
      <ProductResources
        productId={PRODUCT_ID}
        listResources={listResources}
        addResource={addResource}
      />,
    );

    await screen.findByText(/No resources yet/i);
    await userEvent.click(screen.getByRole("button", { name: /Add resource/i }));

    // The add form's Title field appears in the modal.
    const title = await screen.findByLabelText(/^Title$/i);
    await userEvent.type(title, "New doc");
    await userEvent.click(screen.getByRole("button", { name: /^Add$/i }));

    await waitFor(() => {
      expect(addResource).toHaveBeenCalledWith(
        PRODUCT_ID,
        expect.objectContaining({ title: "New doc" }),
      );
    });
    // List re-read → the new resource shows.
    await waitFor(() => expect(listResources).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("New doc")).toBeInTheDocument();
  });

  it("removes a resource via the remove affordance and re-reads", async () => {
    const listResources = vi.fn().mockResolvedValueOnce([resource()]).mockResolvedValueOnce([]);
    const removeResource = vi.fn().mockResolvedValue(undefined);

    render(
      <ProductResources
        productId={PRODUCT_ID}
        listResources={listResources}
        removeResource={removeResource}
      />,
    );

    await screen.findByText("Main repo");
    await userEvent.click(screen.getByRole("button", { name: /Remove/i }));

    await waitFor(() =>
      expect(removeResource).toHaveBeenCalledWith(
        PRODUCT_ID,
        "22222222-2222-2222-2222-222222222222",
      ),
    );
    await waitFor(() => expect(listResources).toHaveBeenCalledTimes(2));
  });
});

describe("Add resource form", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("submits addResource with {kind, title, url, note} and calls onAdded", async () => {
    const addResource = vi
      .fn<(id: string, input: ProductResourceCreate) => Promise<ProductResource>>()
      .mockResolvedValue(resource());
    const onAdded = vi.fn();
    render(<AddResourceForm productId={PRODUCT_ID} onAdded={onAdded} addResource={addResource} />);

    await userEvent.type(screen.getByLabelText(/^Title$/i), "Main repo");
    await userEvent.type(screen.getByLabelText(/^URL$/i), "https://github.com/acme/blog");
    await userEvent.click(screen.getByRole("button", { name: /^Add$/i }));

    await waitFor(() =>
      expect(addResource).toHaveBeenCalledWith(
        PRODUCT_ID,
        expect.objectContaining({ title: "Main repo", url: "https://github.com/acme/blog" }),
      ),
    );
    expect(onAdded).toHaveBeenCalled();
  });

  it("blocks submit on a blank title — no request fired", async () => {
    const addResource = vi.fn();
    render(<AddResourceForm productId={PRODUCT_ID} onAdded={() => {}} addResource={addResource} />);

    await userEvent.click(screen.getByRole("button", { name: /^Add$/i }));
    expect(addResource).not.toHaveBeenCalled();
  });

  it("shows a calm inline error on a failed add and keeps the form usable", async () => {
    const addResource = vi
      .fn<(id: string, input: ProductResourceCreate) => Promise<ProductResource>>()
      .mockRejectedValue(new Error("500"));
    render(<AddResourceForm productId={PRODUCT_ID} onAdded={() => {}} addResource={addResource} />);

    await userEvent.type(screen.getByLabelText(/^Title$/i), "x");
    await userEvent.click(screen.getByRole("button", { name: /^Add$/i }));

    expect(await screen.findByText(/Couldn.t add/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Add$/i })).toBeEnabled();
  });
});
