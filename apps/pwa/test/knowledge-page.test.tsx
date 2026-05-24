/**
 * Knowledge surface — the read-only knowledge view (formerly "Inside"), driven
 * by a route-aware mocked fetch. The primary view is a force-directed graph;
 * the Concepts + Observations lists sit below it. Asserts:
 *  - the graph container renders when the graph has nodes
 *  - the graph's calm empty state ("No connections yet") when it has none
 *  - a calm inline error on a failed graph read — never a crash
 *  - both lists still render the REAL fields (concept name + summary + alias
 *    count; observation title + excerpt + tags)
 *  - the calm "nothing learned" state when the workspace has learned nothing yet
 *  - a calm inline error (not a blank page) when a list read fails
 *
 * `react-force-graph-2d` needs a real canvas (jsdom has none), so it is mocked
 * to a simple stub that just renders its node count — enough to assert the
 * container mounts without choking the test environment.
 */

import Knowledge from "@/components/knowledge/Knowledge";
import type { Concept, ConceptDetail, KnowledgeGraph, Observation } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The canvas lib reaches for a real canvas — stub it. The stub renders a marker
// + the node count so the test can assert the canvas container mounted.
vi.mock("react-force-graph-2d", () => ({
  default: ({ graphData }: { graphData: { nodes: unknown[] } }) => (
    <div data-testid="force-graph-stub">nodes:{graphData.nodes.length}</div>
  ),
}));

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const CONCEPT: Concept = {
  id: "self-hosting",
  name: "Self-hosting",
  summary: "Running services on owned hardware instead of a managed cloud.",
  aliases: ["self host", "selfhosting"],
  alias_count: 2,
  created_at: "2026-05-22T00:00:00Z",
  updated_at: "2026-05-23T00:00:00Z",
};

const OBSERVATION: Observation = {
  id: "garden/seedling/2026-05-23-related-posts.md",
  title: "Related posts widget shows 5 items",
  excerpt: "Founder settled on 5 over 3; both fit the layout.",
  tags: ["frontend", "widget"],
  captured_at: "2026-05-23T00:00:00Z",
};

const GRAPH: KnowledgeGraph = {
  nodes: [
    { id: "a", label: "Auth", kind: "concept", weight: 1 },
    { id: "b", label: "JWKS", kind: "concept", weight: 1 },
  ],
  edges: [{ source: "a", target: "b", type: "relates_to", weight: 0.8 }],
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** A route-aware fetch mock for the Knowledge reads. The optional `detail`
 *  handler serves the per-concept inspector read; the `/concepts/{id}` check
 *  MUST precede the `/concepts` list check (the list path is a prefix). */
function installFetch(opts: {
  concepts: () => Concept[] | Response;
  observations: () => Observation[] | Response;
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
    if (url.startsWith("/api/v1/inside/concepts")) {
      const c = opts.concepts();
      return c instanceof Response ? c : json(c);
    }
    if (url.startsWith("/api/v1/inside/observations")) {
      const o = opts.observations();
      return o instanceof Response ? o : json(o);
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  global.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

describe("Knowledge surface", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the graph container when the graph has nodes", async () => {
    installFetch({
      concepts: () => [CONCEPT],
      observations: () => [OBSERVATION],
      graph: () => GRAPH,
    });

    render(<Knowledge />);

    await waitFor(() => {
      expect(screen.getByTestId("knowledge-graph-canvas")).toBeInTheDocument();
    });
    // The (mocked) force graph mounts with the two nodes — it loads via
    // next/dynamic (ssr:false), so it resolves on a microtask after the
    // container mounts.
    await waitFor(() => {
      expect(screen.getByTestId("force-graph-stub")).toHaveTextContent("nodes:2");
    });
  });

  it("shows the calm graph empty state when there are no connections", async () => {
    installFetch({
      concepts: () => [CONCEPT],
      observations: () => [OBSERVATION],
      graph: () => ({ nodes: [], edges: [] }),
    });

    render(<Knowledge />);

    await waitFor(() => {
      expect(screen.getByTestId("knowledge-graph-empty")).toBeInTheDocument();
    });
    expect(screen.getByText(/No connections yet/)).toBeInTheDocument();
    // The canvas is not mounted when empty.
    expect(screen.queryByTestId("knowledge-graph-canvas")).not.toBeInTheDocument();
  });

  it("shows a calm inline error when the graph read fails — never a crash", async () => {
    installFetch({
      concepts: () => [CONCEPT],
      observations: () => [OBSERVATION],
      graph: () => json("boom", 500),
    });

    render(<Knowledge />);

    await waitFor(() => {
      expect(screen.getByText(/Couldn’t draw the knowledge graph/)).toBeInTheDocument();
    });
    // The lists still render — a failed graph never blanks the surface.
    expect(screen.getByText(CONCEPT.name)).toBeInTheDocument();
  });

  it("renders both lists with their real fields below the graph", async () => {
    installFetch({
      concepts: () => [CONCEPT],
      observations: () => [OBSERVATION],
      graph: () => GRAPH,
    });

    render(<Knowledge />);

    await waitFor(() => {
      expect(screen.getByText(CONCEPT.name)).toBeInTheDocument();
    });
    expect(screen.getByRole("region", { name: "What I know" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Recently observed" })).toBeInTheDocument();

    expect(screen.getByText(CONCEPT.summary)).toBeInTheDocument();
    expect(screen.getByText(/2 mentions/)).toBeInTheDocument();
    expect(screen.getByText(OBSERVATION.title)).toBeInTheDocument();
    expect(screen.getByText(OBSERVATION.excerpt)).toBeInTheDocument();
    expect(screen.getByText("frontend")).toBeInTheDocument();
  });

  it("shows the calm 'nothing learned' list state when nothing has been learned", async () => {
    installFetch({
      concepts: () => [],
      observations: () => [],
      graph: () => ({ nodes: [], edges: [] }),
    });

    render(<Knowledge />);

    await waitFor(() => {
      expect(screen.getByText(/I haven’t learned anything yet/)).toBeInTheDocument();
    });
    expect(screen.queryByRole("region", { name: "What I know" })).not.toBeInTheDocument();
  });

  it("renders concepts even when observations fail — calm, not blank", async () => {
    installFetch({
      concepts: () => [CONCEPT],
      observations: () => json("boom", 500),
      graph: () => GRAPH,
    });

    render(<Knowledge />);

    await waitFor(() => {
      expect(screen.getByText(CONCEPT.name)).toBeInTheDocument();
    });
    expect(screen.getByRole("region", { name: "What I know" })).toBeInTheDocument();
    expect(screen.getByText(/Couldn’t load recent observations/)).toBeInTheDocument();
  });

  const OTHER_CONCEPT: Concept = {
    id: "vaultwarden",
    name: "Vaultwarden",
    summary: "Self-hosted password manager.",
    aliases: [],
    alias_count: 0,
    created_at: "2026-05-22T00:00:00Z",
    updated_at: "2026-05-23T00:00:00Z",
  };

  const DETAIL: ConceptDetail = {
    id: "self-hosting",
    name: "Self-hosting",
    aliases: ["self host"],
    related: [{ id: "vaultwarden", name: "Vaultwarden", weight: 1 }],
    observations: [
      {
        id: "garden/seedling/obs.md",
        title: "Moved the vault to my mini",
        excerpt: "Cutover went clean.",
        captured_at: "2026-05-20",
      },
    ],
  };

  it("filters the 'What I know' list by the search box", async () => {
    installFetch({
      concepts: () => [CONCEPT, OTHER_CONCEPT],
      observations: () => [OBSERVATION],
      graph: () => GRAPH,
    });

    render(<Knowledge />);

    await waitFor(() => {
      expect(screen.getByText(CONCEPT.name)).toBeInTheDocument();
    });
    expect(screen.getByText(OTHER_CONCEPT.name)).toBeInTheDocument();

    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "vault" } });

    // The non-matching concept is filtered out; the matching one stays.
    expect(screen.queryByText(CONCEPT.name)).not.toBeInTheDocument();
    expect(screen.getByText(OTHER_CONCEPT.name)).toBeInTheDocument();
  });

  it("opens the inspector when a concept in the list is clicked", async () => {
    installFetch({
      concepts: () => [CONCEPT],
      observations: () => [OBSERVATION],
      graph: () => GRAPH,
      detail: () => DETAIL,
    });

    render(<Knowledge />);

    await waitFor(() => {
      expect(screen.getByText(CONCEPT.name)).toBeInTheDocument();
    });
    // The concept row is a button that opens the inspector.
    fireEvent.click(screen.getByRole("button", { name: new RegExp(CONCEPT.name) }));

    await waitFor(() => {
      expect(screen.getByRole("complementary", { name: /concept/i })).toBeInTheDocument();
    });
    // The inspector shows the detail's source observation.
    expect(await screen.findByText("Moved the vault to my mini")).toBeInTheDocument();
  });
});
