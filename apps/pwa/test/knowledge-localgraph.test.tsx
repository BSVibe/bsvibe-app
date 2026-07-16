/**
 * Knowledge surface — Lift 5 local-graph view. Selecting a concept drops the
 * canvas out of the global concept hairball into a focused 1-hop local graph
 * (focus concept + related concepts + member observations as seedling leaves),
 * which is the navigable exploration surface; a "full graph" control returns to
 * the global overview. The global graph stays concept-only — seedlings only
 * appear in the local view, never flat in the overview.
 *
 * Same canvas stub as knowledge-page.test.tsx: a button per node wired to the
 * real onNodeClick + a node count, so a "tap" and the active graphData are
 * observable without a real canvas.
 */

import Knowledge from "@/components/knowledge/Knowledge";
import type { ConceptDetail, KnowledgeGraph } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type StubNode = {
  id: string;
  name?: string;
  label?: string;
  nodeType?: string;
  x?: number;
  y?: number;
};
interface StubProps {
  graphData: { nodes: StubNode[] };
  onNodeClick?: (node: StubNode) => void;
  onBackgroundClick?: (event: MouseEvent) => void;
}
// `Knowledge` defers `KnowledgeGraphView` behind `next/dynamic({ ssr: false })`
// — a runtime `import()` that is the ONLY wall-clock-unbounded async in this
// test (the canvas is already a synchronous DOM stub below, and the fetch is a
// mocked microtask). Under a saturated parallel suite vite's on-demand transform
// of the view + its heavy deps (react-markdown/remark-gfm/d3-force) stretches
// that boundary toward the default 1s findBy window (measured ~209ms isolated →
// 600ms+ under the full suite; a weak 2-core CI runner crosses 1000ms → flake).
// #519 only widened the window on the sibling knowledge-page test. Render the
// view synchronously instead, so the test's async is purely microtask-bound and
// deterministic regardless of machine load. Scoped to this file: the Knowledge
// surface is the only `next/dynamic` consumer under test here.
vi.mock("next/dynamic", async () => {
  const mod = await import("@/components/knowledge/KnowledgeGraphView");
  return { default: () => mod.default };
});

// Self-imports react so it does not depend on the test module's top-level
// import init order — the `next/dynamic` mock above eagerly evaluates
// `KnowledgeGraphView` (which imports this) during mock setup, before those
// bindings initialize.
vi.mock("react-force-graph-2d", async () => {
  const { forwardRef, useImperativeHandle } = await import("react");
  return {
    default: forwardRef<unknown, StubProps>((props, ref) => {
      props.graphData.nodes.forEach((n, i) => {
        n.x = i * 100;
        n.y = 0;
      });
      useImperativeHandle(ref, () => ({
        screen2GraphCoords: (x: number, _y: number) => ({ x, y: 0 }),
        d3Force: () => undefined,
      }));
      return (
        <div data-testid="force-graph-stub">
          nodes:{props.graphData.nodes.length}
          {props.graphData.nodes.map((n) => (
            <button
              key={n.id}
              type="button"
              data-testid={`graph-node-${n.id}`}
              data-node-type={n.nodeType ?? ""}
              onClick={() => props.onNodeClick?.(n)}
            >
              {n.name ?? n.label ?? n.id}
            </button>
          ))}
        </div>
      );
    }),
  };
});

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const GRAPH: KnowledgeGraph = {
  nodes: [
    { id: "auth", label: "Auth", kind: "concept", community: "auth-domain", weight: 2 },
    { id: "jwks", label: "JWKS", kind: "concept", community: "auth-domain", weight: 1 },
    { id: "deploy", label: "Deploy", kind: "topic", community: "deploy", weight: 1 },
  ],
  edges: [{ source: "auth", target: "jwks", type: "relates_to", weight: 0.8 }],
};

const AUTH_DETAIL: ConceptDetail = {
  id: "auth",
  name: "Auth",
  aliases: [],
  related: [
    { id: "jwks", name: "JWKS", weight: 2 },
    { id: "session", name: "Session", weight: 1 },
  ],
  observations: [
    {
      id: "garden/seedling/auth.md",
      title: "Wired the auth callback",
      excerpt: "redirect confirmed",
      body: "redirect confirmed",
      truncated: false,
      captured_at: "2026-05-21",
    },
  ],
  type: "concept",
};

const JWKS_DETAIL: ConceptDetail = {
  id: "jwks",
  name: "JWKS",
  aliases: [],
  related: [{ id: "auth", name: "Auth", weight: 2 }],
  observations: [],
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installFetch(detailFn: (id: string) => ConceptDetail | Response) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/v1/inside/graph")) return json(GRAPH);
    const m = url.match(/\/api\/v1\/inside\/concepts\/([^?]+)/);
    if (m) {
      const id = decodeURIComponent(m[1]);
      const d = detailFn(id);
      return d instanceof Response ? d : json(d);
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  global.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

describe("Knowledge surface — local-graph view (Lift 5)", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("starts in the global overview (3 concept nodes, no seedlings flat)", async () => {
    installFetch((id) => (id === "auth" ? AUTH_DETAIL : json("nf", 404)));
    const { container } = render(<Knowledge />);

    await waitFor(() => {
      expect(screen.getByTestId("force-graph-stub")).toHaveTextContent("nodes:3");
    });
    expect(container.querySelector('[data-view-mode="global"]')).not.toBeNull();
    // No seedling node leaks into the global overview.
    expect(screen.queryByTestId("graph-node-garden/seedling/auth.md")).not.toBeInTheDocument();
  });

  it("switches the canvas to the focused local graph when a concept is selected", async () => {
    installFetch((id) => (id === "auth" ? AUTH_DETAIL : json("nf", 404)));
    const { container } = render(<Knowledge />);

    fireEvent.click(await screen.findByTestId("graph-node-auth"));

    // Local graph = focus (auth) + 2 related concepts + 1 seedling = 4 nodes.
    await waitFor(() => {
      expect(screen.getByTestId("force-graph-stub")).toHaveTextContent("nodes:4");
    });
    expect(container.querySelector('[data-view-mode="local"]')).not.toBeNull();
    // The member observation surfaces as a seedling leaf in the local view.
    const seed = screen.getByTestId("graph-node-garden/seedling/auth.md");
    expect(seed).toHaveAttribute("data-node-type", "seedling");
    // The deploy concept (not a neighbour of auth) is gone from the local view.
    expect(screen.queryByTestId("graph-node-deploy")).not.toBeInTheDocument();
  });

  it("returns to the global overview via the full-graph control", async () => {
    installFetch((id) => (id === "auth" ? AUTH_DETAIL : json("nf", 404)));
    const { container } = render(<Knowledge />);

    fireEvent.click(await screen.findByTestId("graph-node-auth"));
    await waitFor(() => {
      expect(container.querySelector('[data-view-mode="local"]')).not.toBeNull();
    });

    fireEvent.click(screen.getByRole("button", { name: /full graph/i }));

    await waitFor(() => {
      expect(container.querySelector('[data-view-mode="global"]')).not.toBeNull();
    });
    expect(screen.getByTestId("force-graph-stub")).toHaveTextContent("nodes:3");
  });

  it("pivots the local graph onto a related concept clicked in the canvas", async () => {
    installFetch((id) =>
      id === "auth" ? AUTH_DETAIL : id === "jwks" ? JWKS_DETAIL : json("nf", 404),
    );
    render(<Knowledge />);

    fireEvent.click(await screen.findByTestId("graph-node-auth"));
    await waitFor(() => {
      expect(screen.getByTestId("force-graph-stub")).toHaveTextContent("nodes:4");
    });

    // Click the JWKS *concept* node inside the local canvas → pivot focus.
    fireEvent.click(screen.getByTestId("graph-node-jwks"));

    // JWKS local graph = focus (jwks) + 1 related (auth) + 0 seedlings = 2 nodes.
    await waitFor(() => {
      expect(screen.getByTestId("force-graph-stub")).toHaveTextContent("nodes:2");
    });
    expect(screen.getByRole("heading", { name: "JWKS" })).toBeInTheDocument();
  });

  it("does not pivot (nor 404) when a seedling leaf is clicked — it is not a concept", async () => {
    const detailFn = vi.fn((id: string) => (id === "auth" ? AUTH_DETAIL : json("nf", 404)));
    installFetch(detailFn);
    render(<Knowledge />);

    fireEvent.click(await screen.findByTestId("graph-node-auth"));
    await screen.findByRole("heading", { name: "Auth" });
    const callsAfterSelect = detailFn.mock.calls.length;

    fireEvent.click(screen.getByTestId("graph-node-garden/seedling/auth.md"));

    // No new getConceptDetail fetch (a seedling id would 404), panel stays on Auth.
    await waitFor(() => {
      expect(detailFn.mock.calls.length).toBe(callsAfterSelect);
    });
    expect(screen.getByRole("heading", { name: "Auth" })).toBeInTheDocument();
    expect(screen.queryByText(/don’t know that concept/i)).not.toBeInTheDocument();
  });
});
