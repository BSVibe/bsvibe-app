/**
 * BootstrapStatusPanel — calm progress panel for a Product whose
 * `bootstrap_status` is non-null and not yet `complete`.
 *
 *  - Renders the matching status line for each lifecycle stage.
 *  - Renders nothing when status is `null` or `complete`.
 *  - Renders the amber failure variant with error detail on `failed:*`.
 *  - Polls the endpoint until a terminal state is reached.
 */

import BootstrapStatusPanel from "@/components/products/BootstrapStatusPanel";
import type { ProductBootstrap } from "@/lib/api/types";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

function snapshot(overrides: Partial<ProductBootstrap> = {}): ProductBootstrap {
  return {
    product_id: "p1",
    status: "analyzing",
    artifacts_count: null,
    error: null,
    run_id: null,
    started_at: "2026-06-03T00:00:00Z",
    completed_at: null,
    progress: null,
    ...overrides,
  };
}

describe("BootstrapStatusPanel", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders the analyzing line when status is analyzing", async () => {
    const getBootstrap = vi.fn().mockResolvedValue(snapshot({ status: "analyzing" }));
    render(<BootstrapStatusPanel productId="p1" getBootstrap={getBootstrap} />);
    expect(await screen.findByText(/Analyzing the repository/i)).toBeInTheDocument();
  });

  it("renders the cloning line when status is cloning", async () => {
    const getBootstrap = vi.fn().mockResolvedValue(snapshot({ status: "cloning" }));
    render(<BootstrapStatusPanel productId="p1" getBootstrap={getBootstrap} />);
    expect(await screen.findByText(/Cloning the repository/i)).toBeInTheDocument();
  });

  it("renders nothing when status is null", async () => {
    const getBootstrap = vi.fn().mockResolvedValue(snapshot({ status: null }));
    const { container } = render(
      <BootstrapStatusPanel productId="p1" getBootstrap={getBootstrap} />,
    );
    await waitFor(() => expect(getBootstrap).toHaveBeenCalled());
    expect(container.querySelector(".bootstrap-status")).toBeNull();
  });

  it("renders nothing when status is complete", async () => {
    const getBootstrap = vi.fn().mockResolvedValue(snapshot({ status: "complete" }));
    const { container } = render(
      <BootstrapStatusPanel productId="p1" getBootstrap={getBootstrap} />,
    );
    await waitFor(() => expect(getBootstrap).toHaveBeenCalled());
    expect(container.querySelector(".bootstrap-status")).toBeNull();
  });

  it("renders failed variant with error detail on failed:clone", async () => {
    const getBootstrap = vi
      .fn()
      .mockResolvedValue(snapshot({ status: "failed:clone", error: "GitError: clone refused" }));
    render(<BootstrapStatusPanel productId="p1" getBootstrap={getBootstrap} />);
    expect(await screen.findByText(/Couldn’t clone that repository/i)).toBeInTheDocument();
    expect(screen.getByText(/GitError: clone refused/)).toBeInTheDocument();
  });

  it("renders the too-large failure variant", async () => {
    const getBootstrap = vi
      .fn()
      .mockResolvedValue(snapshot({ status: "failed:too_large", error: "files=10001" }));
    render(<BootstrapStatusPanel productId="p1" getBootstrap={getBootstrap} />);
    expect(await screen.findByText(/too large to analyze/i)).toBeInTheDocument();
  });

  it("renders the chunk progress line when ingesting with progress data", async () => {
    const getBootstrap = vi.fn().mockResolvedValue(
      snapshot({
        status: "ingesting",
        progress: {
          chunks_done: 12,
          chunks_total: 47,
          chunks_failed: 0,
          notes_created: 30,
          notes_updated: 5,
          phase: "ingesting",
        },
      }),
    );
    render(<BootstrapStatusPanel productId="p1" getBootstrap={getBootstrap} />);
    expect(await screen.findByText(/Ingesting 12 \/ 47 chunks/i)).toBeInTheDocument();
  });

  it("renders the chunk progress with failures count when chunks_failed > 0", async () => {
    const getBootstrap = vi.fn().mockResolvedValue(
      snapshot({
        status: "ingesting",
        progress: {
          chunks_done: 20,
          chunks_total: 47,
          chunks_failed: 3,
          notes_created: 50,
          notes_updated: 8,
          phase: "ingesting",
        },
      }),
    );
    render(<BootstrapStatusPanel productId="p1" getBootstrap={getBootstrap} />);
    expect(
      await screen.findByText(/Ingesting 20 \/ 47 chunks \(3 failed\)/i),
    ).toBeInTheDocument();
  });

  it("does not render the chunk progress line when progress is null", async () => {
    const getBootstrap = vi
      .fn()
      .mockResolvedValue(snapshot({ status: "ingesting", progress: null }));
    render(<BootstrapStatusPanel productId="p1" getBootstrap={getBootstrap} />);
    // The status line still renders, but no progress detail.
    expect(await screen.findByText(/Indexing the repository/i)).toBeInTheDocument();
    expect(screen.queryByText(/chunks/i)).toBeNull();
  });
});
