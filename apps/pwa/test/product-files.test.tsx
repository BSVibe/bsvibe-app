/**
 * Product "Files" viewer (components/products/ProductFiles.tsx) — a lazy
 * file-tree browser over the product's git main. Verified here:
 *  - the root level renders (dirs + files) on mount
 *  - a folder expands LAZILY (children fetched only on first open, with the
 *    subdir path) and collapses
 *  - selecting a file fetches + renders its content from the product endpoint
 *  - a failed content read degrades to a calm note (no blank, no throw)
 */

import ProductFiles from "@/components/products/ProductFiles";
import type { FileTreeEntry, ProductFileContent } from "@/lib/api/types";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const ROOT: FileTreeEntry[] = [
  { name: "src", path: "src", kind: "dir" },
  { name: "README.md", path: "README.md", kind: "file" },
];
const SRC: FileTreeEntry[] = [{ name: "app.py", path: "src/app.py", kind: "file" }];

afterEach(() => vi.restoreAllMocks());

it("renders the root level on mount", async () => {
  const listFiles = vi.fn(async () => ROOT);
  render(<ProductFiles productId="p1" listFiles={listFiles} getContent={vi.fn()} />);

  await waitFor(() => expect(screen.getByRole("button", { name: /src/ })).toBeInTheDocument());
  expect(screen.getByRole("button", { name: "README.md" })).toBeInTheDocument();
  // Lazy: only the root was fetched (no eager walk into src/).
  expect(listFiles).toHaveBeenCalledTimes(1);
  expect(listFiles).toHaveBeenCalledWith("p1");
});

it("expands a folder lazily and fetches its children with the subdir path", async () => {
  const listFiles = vi.fn(async (_pid: string, path = "") => (path === "src" ? SRC : ROOT));
  render(<ProductFiles productId="p1" listFiles={listFiles} getContent={vi.fn()} />);

  await waitFor(() => screen.getByRole("button", { name: /src/ }));
  // app.py isn't fetched until the folder is opened.
  expect(screen.queryByRole("button", { name: "app.py" })).not.toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: /src/ }));
  await waitFor(() => expect(screen.getByRole("button", { name: "app.py" })).toBeInTheDocument());
  expect(listFiles).toHaveBeenCalledWith("p1", "src");
});

it("fetches + renders a file's content from the product endpoint", async () => {
  const content: ProductFileContent = {
    path: "README.md",
    content: "# hello world",
    truncated: false,
    binary: false,
  };
  const getContent = vi.fn(async () => content);
  render(
    <ProductFiles productId="p1" listFiles={vi.fn(async () => ROOT)} getContent={getContent} />,
  );

  await waitFor(() => screen.getByRole("button", { name: "README.md" }));
  await userEvent.click(screen.getByRole("button", { name: "README.md" }));

  await waitFor(() => expect(screen.getByText("# hello world")).toBeInTheDocument());
  expect(getContent).toHaveBeenCalledWith("p1", "README.md");
});

it("degrades a failed content read to a calm note", async () => {
  const getContent = vi.fn(async () => {
    throw new Error("404");
  });
  render(
    <ProductFiles productId="p1" listFiles={vi.fn(async () => ROOT)} getContent={getContent} />,
  );

  await waitFor(() => screen.getByRole("button", { name: "README.md" }));
  await userEvent.click(screen.getByRole("button", { name: "README.md" }));
  await waitFor(() => expect(screen.getByText(/Couldn.t open this file/i)).toBeInTheDocument());
});

it("shows a calm empty state when the repo lists nothing", async () => {
  render(<ProductFiles productId="p1" listFiles={vi.fn(async () => [])} getContent={vi.fn()} />);
  await waitFor(() => expect(screen.getByText(/No files produced/i)).toBeInTheDocument());
});
