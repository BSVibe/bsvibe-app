/**
 * Knowledge surface — the BSage-style knowledge-graph view (ported from
 * BSage's `KnowledgeGraphView`). The graph IS the surface now: there is no
 * separate "What I know" list and no "Recently observed" section (directive
 * #2 — removed as noise; the system learns from everything). Asserts:
 *  - the graph canvas mounts when `/inside/graph` has nodes
 *  - tapping a graph node fetches `getConceptDetail` and opens the detail panel
 *    (directive #1 — clicks now work via the canvas hit-area; here we drive the
 *    onNodeClick the lib provides)
 *  - the detail panel renders the concept's name, aliases, related concepts
 *    (clickable pivots), and source observations
 *  - clicking a related concept pivots the panel onto the neighbour
 *  - the search box filters the graph nodes
 *  - the group(kind) legend lets you toggle a group off (filters its nodes)
 *  - the "Recently observed" section is GONE
 *  - a calm empty state on a fresh/sparse workspace
 *  - a calm inline error on a failed graph read — never a crash
 *
 * `react-force-graph-2d` needs a real canvas (jsdom has none), so it is mocked
 * to a stub that exposes a button per node wired to the real `onNodeClick`, so
 * a "node tap" is simulated exactly the way the lib invokes it.
 */

import Knowledge from "@/components/knowledge/Knowledge";
import type { ConceptDetail, KnowledgeGraph } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { forwardRef, useImperativeHandle } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Stub the canvas lib: render a marker, the node count, and a button per node
// wired to the real onNodeClick (node object → onNodeClick(node)) so a tap is
// reproducible. Also capture all props so we can assert the click hit-area is
// wired (nodePointerAreaPaint) AND the hover-independent canvas click path
// (onBackgroundClick + screen2GraphCoords via the fg ref).
//
// The stub also forwards the lib's `ref` to a fake ForceGraph2D instance that
// exposes `screen2GraphCoords(x, y)` — the explicit click path under test. We
// give each node a deterministic x/y (its index) so the nearest-node math is
// reproducible, and translate a click at "screen" coords straight through.
type StubNode = { id: string; name?: string; label?: string; x?: number; y?: number };
interface StubProps {
  graphData: { nodes: StubNode[] };
  onNodeClick?: (node: StubNode) => void;
  onBackgroundClick?: (event: MouseEvent) => void;
}
let captured: Record<string, unknown> = {};
// The real `react-force-graph-2d` is a forwardRef component exposing imperative
// methods (incl. screen2GraphCoords). The mock mirrors that so the component's
// `fgRef.current` is populated — otherwise the hover-independent click path
// (which reads `fgRef.current.screen2GraphCoords`) can never fire.
vi.mock("react-force-graph-2d", () => ({
  default: forwardRef<unknown, StubProps>((props, ref) => {
    captured = props as unknown as Record<string, unknown>;
    // Lay nodes out on a deterministic grid so the component's nearest-node
    // hit-test (screen2GraphCoords → distance ≤ radius) is reproducible.
    props.graphData.nodes.forEach((n, i) => {
      n.x = i * 100;
      n.y = 0;
    });
    useImperativeHandle(ref, () => ({
      // screen2GraphCoords echoes the click coords straight through (the real
      // lib returns DPR-correct graph coords; the component must use them as
      // graph coords for the nearest-node search).
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
            onClick={() => props.onNodeClick?.(n)}
          >
            {n.name ?? n.label ?? n.id}
          </button>
        ))}
        {/* A background-click target carrying offset coords, to drive the
            hover-independent screen2GraphCoords path under test. */}
        <button
          type="button"
          data-testid="graph-background"
          onClick={(e) => {
            const native = e.nativeEvent as MouseEvent;
            Object.defineProperty(native, "offsetX", { value: 0, configurable: true });
            Object.defineProperty(native, "offsetY", { value: 0, configurable: true });
            props.onBackgroundClick?.(native);
          }}
        >
          bg
        </button>
      </div>
    );
  }),
}));

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
  edges: [
    { source: "auth", target: "jwks", type: "relates_to", weight: 0.8 },
    { source: "auth", target: "deploy", type: "relates_to", weight: 0.5 },
  ],
};

const AUTH_DETAIL: ConceptDetail = {
  id: "auth",
  name: "Auth",
  aliases: ["authn", "authentication"],
  related: [{ id: "jwks", name: "JWKS", weight: 2 }],
  observations: [
    {
      id: "garden/seedling/auth.md",
      title: "Wired the auth callback",
      excerpt: "Founder confirmed the redirect target.",
      body: "Founder confirmed the redirect target.\n\nThe callback now lands on /app.",
      truncated: false,
      captured_at: "2026-05-21",
    },
  ],
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

/** Route-aware fetch mock for the Knowledge reads. The `/concepts/{id}` detail
 *  check MUST precede the list-prefix check (there is no list call anymore, but
 *  the matcher mirrors the real client paths). */
function installFetch(opts: {
  graph: () => KnowledgeGraph | Response;
  detail?: (id: string) => ConceptDetail | Response;
}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/v1/inside/graph")) {
      const g = opts.graph();
      return g instanceof Response ? g : json(g);
    }
    const detailMatch = url.match(/\/api\/v1\/inside\/concepts\/([^?]+)/);
    if (detailMatch) {
      const id = decodeURIComponent(detailMatch[1]);
      const d = opts.detail?.(id) ?? json("not found", 404);
      return d instanceof Response ? d : json(d);
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  global.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

describe("Knowledge surface (BSage graph)", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
    captured = {};
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("mounts the graph canvas when the graph has nodes", async () => {
    installFetch({ graph: () => GRAPH });
    render(<Knowledge />);

    await waitFor(() => {
      expect(screen.getByTestId("force-graph-stub")).toHaveTextContent("nodes:3");
    });
  });

  it("wires a node-click hit-area (nodePointerAreaPaint) — the dead-click fix", async () => {
    installFetch({ graph: () => GRAPH });
    render(<Knowledge />);

    await waitFor(() => {
      expect(screen.getByTestId("force-graph-stub")).toBeInTheDocument();
    });
    // The root cause of dead clicks: a custom nodeCanvasObject with NO
    // nodePointerAreaPaint means the lib's hit-area doesn't match the drawn
    // nodes. The ported component MUST supply it.
    expect(captured.nodePointerAreaPaint).toBeTypeOf("function");
    expect(captured.onNodeClick).toBeTypeOf("function");
  });

  it("opens the detail panel and fetches getConceptDetail when a node is tapped", async () => {
    installFetch({
      graph: () => GRAPH,
      detail: (id) => (id === "auth" ? AUTH_DETAIL : json("not found", 404)),
    });
    render(<Knowledge />);

    const node = await screen.findByTestId("graph-node-auth");
    // No detail panel before the tap.
    expect(screen.queryByRole("complementary", { name: /concept/i })).not.toBeInTheDocument();

    fireEvent.click(node);

    await waitFor(() => {
      expect(screen.getByRole("complementary", { name: /concept/i })).toBeInTheDocument();
    });
    // Detail panel renders the concept's real fields.
    expect(await screen.findByRole("heading", { name: "Auth" })).toBeInTheDocument();
    expect(screen.getByText("authn")).toBeInTheDocument();
    // The inspector leads with the note's CONTENT (the body, as a readable note),
    // NOT the source detail — the observation title/date are no longer shown.
    expect(screen.getByText(/redirect target/)).toBeInTheDocument();
    expect(screen.getByText(/lands on \/app/)).toBeInTheDocument();
    expect(screen.queryByText("Wired the auth callback")).not.toBeInTheDocument();
    // Related concept rendered as a clickable pivot — scoped to the panel (a
    // graph-node stub button shares the "JWKS" name in the canvas mock).
    const panel = screen.getByRole("complementary", { name: /concept/i });
    expect(within(panel).getByRole("button", { name: /JWKS/ })).toBeInTheDocument();
  });

  it("pivots the panel when a related concept is clicked (re-fetches the neighbour)", async () => {
    installFetch({
      graph: () => GRAPH,
      detail: (id) => (id === "auth" ? AUTH_DETAIL : id === "jwks" ? JWKS_DETAIL : json("nf", 404)),
    });
    render(<Knowledge />);

    fireEvent.click(await screen.findByTestId("graph-node-auth"));
    await screen.findByRole("heading", { name: "Auth" });

    // Click the related-concept chip inside the panel (not the same-named graph
    // node button the canvas stub renders).
    const panel = screen.getByRole("complementary", { name: /concept/i });
    fireEvent.click(within(panel).getByRole("button", { name: /JWKS/ }));

    // The panel pivots onto JWKS (a fresh getConceptDetail("jwks")).
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "JWKS" })).toBeInTheDocument();
    });
  });

  it("filters the graph nodes by the search box", async () => {
    installFetch({ graph: () => GRAPH });
    render(<Knowledge />);

    await waitFor(() => {
      expect(screen.getByTestId("force-graph-stub")).toHaveTextContent("nodes:3");
    });

    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "jwks" } });

    // Only the matching node remains in the graph data.
    await waitFor(() => {
      expect(screen.getByTestId("force-graph-stub")).toHaveTextContent("nodes:1");
    });
    expect(screen.getByTestId("graph-node-jwks")).toBeInTheDocument();
    expect(screen.queryByTestId("graph-node-auth")).not.toBeInTheDocument();
  });

  it("filters by the group(kind) legend toggle", async () => {
    installFetch({ graph: () => GRAPH });
    render(<Knowledge />);

    await waitFor(() => {
      expect(screen.getByTestId("force-graph-stub")).toHaveTextContent("nodes:3");
    });

    // Toggle the "topic" group off — its single node (deploy) drops out.
    fireEvent.click(screen.getByRole("button", { name: /topic/i }));

    await waitFor(() => {
      expect(screen.queryByTestId("graph-node-deploy")).not.toBeInTheDocument();
    });
    expect(screen.getByTestId("graph-node-auth")).toBeInTheDocument();
    expect(screen.getByTestId("graph-node-jwks")).toBeInTheDocument();
  });

  it("renders a full-screen graph container (directive #2 — not a boxed card)", async () => {
    installFetch({ graph: () => GRAPH });
    const { container } = render(<Knowledge />);

    await waitFor(() => {
      expect(screen.getByTestId("force-graph-stub")).toBeInTheDocument();
    });
    // The surface fills the content area via the full-screen modifier (CSS
    // makes `.kgraph--fullscreen` an absolutely-positioned full-bleed layer).
    expect(container.querySelector(".kgraph--fullscreen")).not.toBeNull();
  });

  it("selects a node via the hover-independent canvas click (screen2GraphCoords path)", async () => {
    installFetch({
      graph: () => GRAPH,
      detail: (id) => (id === "auth" ? AUTH_DETAIL : json("not found", 404)),
    });
    render(<Knowledge />);

    await screen.findByTestId("force-graph-stub");
    // The explicit click path MUST be wired (does not rely on the lib's
    // hover-based onNodeClick): an onBackgroundClick handler that uses the fg
    // ref's screen2GraphCoords to find the nearest node.
    expect(captured.onBackgroundClick).toBeTypeOf("function");

    // Click the canvas background at offset (0,0) — the "auth" node sits at
    // graph coords (0,0) in the stub layout, so the nearest-node search selects
    // it and opens the inspector — WITHOUT any prior pointer-move/hover.
    fireEvent.click(screen.getByTestId("graph-background"));

    await waitFor(() => {
      expect(screen.getByRole("complementary", { name: /concept/i })).toBeInTheDocument();
    });
    expect(await screen.findByRole("heading", { name: "Auth" })).toBeInTheDocument();
  });

  it("toggles TYPE / COMMUNITY legend modes and recolors the nodes", async () => {
    installFetch({ graph: () => GRAPH });
    render(<Knowledge />);

    await screen.findByTestId("force-graph-stub");

    // Default mode is TYPE: the legend lists the kinds (Concept / Topic).
    expect(screen.getByRole("button", { name: /concept/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /topic/i })).toBeInTheDocument();

    // Switch to COMMUNITY mode — the legend now lists community groups, and the
    // canvas re-renders with a new nodeCanvasObject (recolors by community).
    fireEvent.click(screen.getByRole("button", { name: /^community$/i }));

    await waitFor(() => {
      // Two communities in the fixture: "auth" (auth+jwks) and "deploy".
      expect(screen.getAllByTestId(/legend-community-/).length).toBe(2);
    });
    // The community legend entry shows the member count (auth-domain has 2).
    expect(screen.getByTestId("legend-community-auth-domain")).toHaveTextContent("2");
    // Lift E29 — communities show the humanized form of the backend's
    // semantic community id (the smallest member concept_id) so the founder
    // can answer "why are these grouped?" at a glance. Pre-E29 this collapsed
    // every community to "Cluster N" which threw away the signal.
    expect(screen.getByTestId("legend-community-auth-domain")).toHaveTextContent("Auth domain");
  });

  it("filters by a COMMUNITY legend entry", async () => {
    installFetch({ graph: () => GRAPH });
    render(<Knowledge />);

    await waitFor(() => {
      expect(screen.getByTestId("force-graph-stub")).toHaveTextContent("nodes:3");
    });

    fireEvent.click(screen.getByRole("button", { name: /^community$/i }));
    // Toggle the "deploy" community off — its single node (deploy) drops out.
    fireEvent.click(await screen.findByTestId("legend-community-deploy"));

    await waitFor(() => {
      expect(screen.queryByTestId("graph-node-deploy")).not.toBeInTheDocument();
    });
    expect(screen.getByTestId("graph-node-auth")).toBeInTheDocument();
    expect(screen.getByTestId("graph-node-jwks")).toBeInTheDocument();
  });

  it("shows the concept TYPE in the inspector metadata", async () => {
    installFetch({ graph: () => GRAPH, detail: () => AUTH_DETAIL });
    render(<Knowledge />);

    fireEvent.click(await screen.findByTestId("graph-node-auth"));
    const panel = await screen.findByRole("complementary", { name: /concept/i });
    // BSage-style metadata: the node's TYPE (kind) is surfaced as a labelled
    // metadatum. The fixture node's kind is "concept" → humanized "Concept".
    expect(within(panel).getByText("Type")).toBeInTheDocument();
    expect(within(panel).getByText("Concept")).toBeInTheDocument();
    // Lift E29 — community is labelled with the humanized form of the
    // backend's semantic community id (the same map the legend uses) so the
    // two surfaces agree. The fixture's "auth-domain" community renders as
    // "Auth domain" (distinct from the node's "Auth" label so the assertion
    // can scope to the metadatum row).
    expect(within(panel).getByText("Community")).toBeInTheDocument();
    expect(within(panel).getByText("Auth domain")).toBeInTheDocument();
  });

  it("no longer renders the 'Recently observed' section (directive #2)", async () => {
    installFetch({ graph: () => GRAPH });
    render(<Knowledge />);

    await waitFor(() => {
      expect(screen.getByTestId("force-graph-stub")).toBeInTheDocument();
    });
    expect(screen.queryByText(/Recently observed/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /Recently observed/i })).not.toBeInTheDocument();
    // And no separate "What I know" list — the graph is the surface now.
    expect(screen.queryByRole("region", { name: /What I know/i })).not.toBeInTheDocument();
  });

  it("shows a calm empty state on a fresh/sparse workspace", async () => {
    installFetch({ graph: () => ({ nodes: [], edges: [] }) });
    render(<Knowledge />);

    await waitFor(() => {
      expect(screen.getByText(/No connections yet/)).toBeInTheDocument();
    });
    expect(screen.queryByTestId("force-graph-stub")).not.toBeInTheDocument();
  });

  it("shows a calm inline error on a failed graph read — never a crash", async () => {
    installFetch({ graph: () => json("boom", 500) });
    render(<Knowledge />);

    await waitFor(() => {
      expect(screen.getByText(/Couldn’t draw the knowledge graph/)).toBeInTheDocument();
    });
  });

  it("closes the detail panel from the close affordance", async () => {
    installFetch({ graph: () => GRAPH, detail: () => AUTH_DETAIL });
    render(<Knowledge />);

    fireEvent.click(await screen.findByTestId("graph-node-auth"));
    await screen.findByRole("complementary", { name: /concept/i });

    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    await waitFor(() => {
      expect(screen.queryByRole("complementary", { name: /concept/i })).not.toBeInTheDocument();
    });
  });
});
