/**
 * New product create form (components/shell/NewProductForm.tsx). A calm form:
 * Name + Slug (auto-suggested from Name, editable). Repo binding is
 * GitHub-connector-only, so there is no repo-URL field. It:
 *
 *  - auto-suggests a valid slug as the founder types the Name
 *  - lets the founder edit the slug, then stops overriding their edit
 *  - blocks submit on an invalid/empty slug (no request fired)
 *  - on submit fires createProduct with {name, slug} and, on 201, navigates to
 *    the new product's /products/{slug}
 *  - shows a calm inline error on failure (e.g. duplicate slug) and keeps the
 *    form usable
 *
 * `createProduct` is injected so the surface is unit-testable against a mock
 * without monkey-patching the module (mirrors AddConnector's createConnector).
 */

import NewProductForm from "@/components/shell/NewProductForm";
import type { Product, ProductCreate } from "@/lib/api/types";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const pushMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/brief",
}));

function product(slug: string, name: string): Product {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    workspace_id: "ws-1",
    name,
    slug,
    repo_url: null,
    bootstrap_status: null,
    bootstrap_artifacts_count: null,
    bootstrap_error: null,
    bootstrap_progress: null,
    created_at: "2026-05-23T00:00:00Z",
    updated_at: "2026-05-23T00:00:00Z",
  };
}

describe("New product form", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    pushMock.mockReset();
  });

  it("auto-suggests a valid slug as the Name is typed", async () => {
    const createProduct = vi.fn();
    render(<NewProductForm onCreated={() => {}} createProduct={createProduct} />);

    await userEvent.type(screen.getByLabelText(/^Name$/i), "Related Posts");

    const slug = screen.getByLabelText(/^Slug$/i) as HTMLInputElement;
    expect(slug.value).toBe("related-posts");
  });

  it("stops auto-suggesting once the founder edits the slug", async () => {
    const createProduct = vi.fn();
    render(<NewProductForm onCreated={() => {}} createProduct={createProduct} />);

    await userEvent.type(screen.getByLabelText(/^Name$/i), "Widgets");
    const slug = screen.getByLabelText(/^Slug$/i) as HTMLInputElement;
    expect(slug.value).toBe("widgets");

    // Founder takes over the slug.
    await userEvent.clear(slug);
    await userEvent.type(slug, "my-widgets");
    // Typing more in Name must NOT clobber the founder's edit.
    await userEvent.type(screen.getByLabelText(/^Name$/i), " Pro");
    expect(slug.value).toBe("my-widgets");
  });

  it("submits createProduct with {name, slug} and navigates on 201", async () => {
    const createProduct = vi
      .fn<(input: ProductCreate) => Promise<Product>>()
      .mockResolvedValue(product("related-posts", "Related Posts"));
    const onCreated = vi.fn();
    render(<NewProductForm onCreated={onCreated} createProduct={createProduct} />);

    await userEvent.type(screen.getByLabelText(/^Name$/i), "Related Posts");
    await userEvent.click(screen.getByRole("button", { name: /Create product/i }));

    await waitFor(() => {
      expect(createProduct).toHaveBeenCalledWith({
        name: "Related Posts",
        slug: "related-posts",
      });
    });
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/products/related-posts"));
    expect(onCreated).toHaveBeenCalled();
  });

  it("Lift A v2 — submits with repo_url when the founder provides one", async () => {
    const createProduct = vi
      .fn<(input: ProductCreate) => Promise<Product>>()
      .mockResolvedValue(product("with-repo", "With Repo"));
    render(<NewProductForm onCreated={() => {}} createProduct={createProduct} />);

    await userEvent.type(screen.getByLabelText(/^Name$/i), "With Repo");
    await userEvent.type(
      screen.getByLabelText(/Git repository URL/i),
      "https://github.com/org/repo",
    );
    await userEvent.click(screen.getByRole("button", { name: /Create product/i }));

    await waitFor(() => {
      expect(createProduct).toHaveBeenCalledWith({
        name: "With Repo",
        slug: "with-repo",
        repo_url: "https://github.com/org/repo",
      });
    });
  });

  it("Lift A v2 — omits repo_url when the founder leaves the field blank", async () => {
    const createProduct = vi
      .fn<(input: ProductCreate) => Promise<Product>>()
      .mockResolvedValue(product("plain", "Plain"));
    render(<NewProductForm onCreated={() => {}} createProduct={createProduct} />);

    await userEvent.type(screen.getByLabelText(/^Name$/i), "Plain");
    await userEvent.click(screen.getByRole("button", { name: /Create product/i }));

    await waitFor(() => {
      expect(createProduct).toHaveBeenCalledWith({ name: "Plain", slug: "plain" });
    });
  });

  it("Lift A v2 — blocks submit on a malformed repo URL", async () => {
    const createProduct = vi.fn();
    render(<NewProductForm onCreated={() => {}} createProduct={createProduct} />);

    await userEvent.type(screen.getByLabelText(/^Name$/i), "With Repo");
    await userEvent.type(screen.getByLabelText(/Git repository URL/i), "ftp://nope/not-http");
    await userEvent.click(screen.getByRole("button", { name: /Create product/i }));

    expect(createProduct).not.toHaveBeenCalled();
    expect(await screen.findByText(/http\(s\):\/\//i)).toBeInTheDocument();
  });

  it("blocks submit on an invalid slug — no request fired", async () => {
    const createProduct = vi.fn();
    render(<NewProductForm onCreated={() => {}} createProduct={createProduct} />);

    await userEvent.type(screen.getByLabelText(/^Name$/i), "Widgets");
    const slug = screen.getByLabelText(/^Slug$/i) as HTMLInputElement;
    // Force an invalid slug (starts with a digit).
    await userEvent.clear(slug);
    await userEvent.type(slug, "1bad");
    await userEvent.click(screen.getByRole("button", { name: /Create product/i }));

    expect(createProduct).not.toHaveBeenCalled();
    expect(await screen.findByText(/starting with a letter/i)).toBeInTheDocument();
  });

  it("shows a calm inline error on a duplicate slug and keeps the form usable", async () => {
    const createProduct = vi
      .fn<(input: ProductCreate) => Promise<Product>>()
      .mockRejectedValue(new Error("409"));
    render(<NewProductForm onCreated={() => {}} createProduct={createProduct} />);

    await userEvent.type(screen.getByLabelText(/^Name$/i), "Widgets");
    await userEvent.click(screen.getByRole("button", { name: /Create product/i }));

    expect(await screen.findByText(/Couldn’t create that product/i)).toBeInTheDocument();
    // Form stays usable (button re-enabled, no navigation).
    expect(screen.getByRole("button", { name: /Create product/i })).toBeEnabled();
    expect(pushMock).not.toHaveBeenCalled();
  });
});
