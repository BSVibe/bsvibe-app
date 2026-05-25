/**
 * Product "Files" viewer (components/products/ProductFiles.tsx). The product
 * detail surface flattens every shipped deliverable's artifact_refs into one
 * list; selecting a file fetches its content from the EXISTING whitelisted
 * artifact endpoint (GET /deliverables/{id}/artifacts/{ref}) and shows it
 * read-only. Verified here:
 *  - a calm empty state when the product produced no files
 *  - files grouped by their producing deliverable
 *  - selecting a file fetches + renders its content from the scoped endpoint
 *  - a binary / failed read degrades to a calm note (no blank, no throw)
 */

import ProductFiles from "@/components/products/ProductFiles";
import type { ArtifactContent, ProductFile } from "@/lib/api/types";
import { setSession } from "@/lib/auth/session";
// @testing-library/react is aliased (vitest.config) to an intl-wrapped render.
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const FILES: ProductFile[] = [
  { id: "d1::fib.py", deliverableId: "d1", deliverableTitle: "Add fib", ref: "fib.py" },
  { id: "d1::src/util.py", deliverableId: "d1", deliverableTitle: "Add fib", ref: "src/util.py" },
  { id: "d2::greet.py", deliverableId: "d2", deliverableTitle: "Add greet", ref: "greet.py" },
];

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderFiles(files: ProductFile[]) {
  return render(<ProductFiles files={files} />);
}

describe("ProductFiles", () => {
  beforeEach(() => {
    setSession({
      accessToken: "tok",
      refreshToken: "ref",
      email: "f@bsvibe.dev",
      userId: "u1",
      expiresAt: Date.now() + 3_600_000,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows a calm empty state when there are no files", () => {
    renderFiles([]);
    expect(screen.getByText(/No files produced/i)).toBeInTheDocument();
  });

  it("lists files grouped by their producing deliverable", () => {
    global.fetch = vi.fn(async () => json({})) as unknown as typeof fetch;
    renderFiles(FILES);

    // Group headers (deliverable titles) + the file leaf names.
    expect(screen.getByText("Add fib")).toBeInTheDocument();
    expect(screen.getByText("Add greet")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "fib.py" })).toBeInTheDocument();
    // Nested ref shows its leaf name, not the full path.
    expect(screen.getByRole("button", { name: "util.py" })).toBeInTheDocument();
  });

  it("fetches + renders a file's content from the scoped artifact endpoint", async () => {
    const content: ArtifactContent = {
      ref: "fib.py",
      content: "def fib(n):\n    return n",
      truncated: false,
      binary: false,
    };
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => json(content));
    global.fetch = fetchMock as unknown as typeof fetch;

    renderFiles(FILES);
    await userEvent.click(screen.getByRole("button", { name: "fib.py" }));

    await waitFor(() => {
      expect(screen.getByText(/def fib\(n\)/)).toBeInTheDocument();
    });
    // Hit the deliverable-scoped, ref-whitelisted endpoint for d1/fib.py.
    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toBe("/api/v1/deliverables/d1/artifacts/fib.py");
  });

  it("degrades a failed content read to a calm note", async () => {
    global.fetch = vi.fn(async () => json("nope", 404)) as unknown as typeof fetch;
    renderFiles(FILES);

    await userEvent.click(screen.getByRole("button", { name: "greet.py" }));
    await waitFor(() => {
      expect(screen.getByText(/Couldn.t open this file/i)).toBeInTheDocument();
    });
  });
});
