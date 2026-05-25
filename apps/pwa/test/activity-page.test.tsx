/**
 * Activity surface — the read-only run-history container, driven by a
 * route-aware mocked fetch. Asserts:
 *  - the runs list renders newest-first with each run's plain-language status
 *  - expanding a run lazily loads + shows that run's deliverable(s): title,
 *    source, "This is verified" verdict, and the external artifact link
 *  - the calm empty state when the workspace has no runs yet
 *  - a calm inline error (not a blank page) when the runs read fails
 *  - a loading note before the read lands
 */

import Activity from "@/components/activity/Activity";
import type { Deliverable, Product, Run } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const NOW = "2026-05-23T00:00:00Z";

const PRODUCT: Product = {
  id: "prod-1",
  workspace_id: "ws-1",
  name: "Blog",
  slug: "blog",
  repo_url: null,
  created_at: NOW,
  updated_at: NOW,
};

const SHIPPED_RUN: Run = {
  id: "run-1",
  workspace_id: "ws-1",
  product_id: "prod-1",
  request_id: "req-1",
  status: "shipped",
  created_at: NOW,
  updated_at: NOW,
};

const RUNNING_RUN: Run = { ...SHIPPED_RUN, id: "run-2", status: "running" };

const DELIVERABLE: Deliverable = {
  id: "d1",
  run_id: "run-1",
  workspace_id: "ws-1",
  deliverable_type: "pr",
  summary: "Add related-posts widget",
  artifact_refs: ["src/posts.ts"],
  artifact_uri: "https://github.com/acme/repo/pull/15",
  created_at: NOW,
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installFetch(opts: {
  runs: () => Run[] | Response;
  products?: () => Product[] | Response;
  deliverables?: () => Deliverable[] | Response;
}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/v1/runs")) {
      const r = opts.runs();
      return r instanceof Response ? r : json(r);
    }
    if (url.startsWith("/api/v1/products")) {
      const p = opts.products?.() ?? [PRODUCT];
      return p instanceof Response ? p : json(p);
    }
    if (url.startsWith("/api/v1/deliverables")) {
      const d = opts.deliverables?.() ?? [];
      return d instanceof Response ? d : json(d);
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  global.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

describe("Activity surface", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the runs newest-first with their plain-language status", async () => {
    installFetch({ runs: () => [SHIPPED_RUN, RUNNING_RUN] });

    render(<Activity />);

    await waitFor(() => {
      expect(screen.getByRole("list", { name: "Recent runs" })).toBeInTheDocument();
    });
    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    // Order preserved (backend already sorts newest-first).
    expect(within(rows[0]).getByText("Shipped")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Working")).toBeInTheDocument();
  });

  it("expands a run to lazily show its deliverable with the verified verdict + link", async () => {
    const fetchMock = installFetch({
      runs: () => [SHIPPED_RUN],
      deliverables: () => [DELIVERABLE],
    });

    render(<Activity />);

    await waitFor(() => {
      expect(screen.getByText("Shipped")).toBeInTheDocument();
    });

    // Deliverables are not fetched until the row is expanded.
    expect(fetchMock.mock.calls.some((c) => String(c[0]).startsWith("/api/v1/deliverables"))).toBe(
      false,
    );

    const toggle = screen.getByRole("button", { expanded: false });
    await userEvent.click(toggle);

    await waitFor(() => {
      expect(screen.getByText("Add related-posts widget")).toBeInTheDocument();
    });
    expect(screen.getByText("opened a pull request")).toBeInTheDocument();
    expect(screen.getByText("This is verified")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /Open artifact/ });
    expect(link).toHaveAttribute("href", "https://github.com/acme/repo/pull/15");
    // A "View report" link opens the deliverable's (redesigned) Delivery Report.
    expect(screen.getByRole("link", { name: /View report/ })).toHaveAttribute(
      "href",
      "/deliverables/d1",
    );
    // The expand fetch narrowed to this run.
    const delUrl = fetchMock.mock.calls
      .map((c) => String(c[0]))
      .find((u) => u.startsWith("/api/v1/deliverables"));
    expect(delUrl).toContain("run_id=run-1");
  });

  it("shows a calm empty message for a run with no deliverables", async () => {
    installFetch({ runs: () => [SHIPPED_RUN], deliverables: () => [] });

    render(<Activity />);
    await waitFor(() => expect(screen.getByText("Shipped")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { expanded: false }));

    await waitFor(() => {
      expect(screen.getByText(/No delivered artifacts for this run/)).toBeInTheDocument();
    });
  });

  it("shows the calm empty state when there are no runs yet", async () => {
    installFetch({ runs: () => [] });

    render(<Activity />);

    await waitFor(() => {
      expect(screen.getByText(/No runs yet/)).toBeInTheDocument();
    });
    expect(screen.getByText(/give me a Direction/i)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Recent runs" })).not.toBeInTheDocument();
  });

  it("renders a calm inline error (not a blank page) when the runs read fails", async () => {
    installFetch({ runs: () => json("boom", 500) });

    render(<Activity />);

    await waitFor(() => {
      expect(screen.getByText(/Couldn’t load recent activity/)).toBeInTheDocument();
    });
    expect(screen.queryByRole("list", { name: "Recent runs" })).not.toBeInTheDocument();
  });

  it("shows a loading note before the read lands", async () => {
    installFetch({ runs: () => [SHIPPED_RUN] });

    render(<Activity />);

    expect(screen.getByText(/Looking at recent activity/)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText("Shipped")).toBeInTheDocument();
    });
  });
});
