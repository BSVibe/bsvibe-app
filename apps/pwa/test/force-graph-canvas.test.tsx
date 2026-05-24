/**
 * ForceGraphCanvas — the client-only force-directed canvas behind the Knowledge
 * graph. These tests pin the node-click path that was silently broken: a graph
 * node tap must call `onNodeClick` with the concept id (the SAME id the list
 * passes to the inspector), and the canvas must be given an explicit, finite
 * size so `react-force-graph-2d`'s shadow-canvas hit-detection lines up with the
 * clipped 360px container instead of defaulting to `window.innerWidth/Height`
 * (the root cause — the visible nodes were drawn outside the readable shadow
 * region, so clicks never resolved to a node).
 *
 * `react-force-graph-2d` needs a real canvas (jsdom has none), so it is mocked
 * to a stub that captures the props the component passes — letting us invoke the
 * captured `onNodeClick` exactly the way the lib would (with the node object)
 * and assert what `width`/`height` it received.
 */

import ForceGraphCanvas from "@/components/knowledge/ForceGraphCanvas";
import type { KnowledgeGraph } from "@/lib/api/types";
import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Capture the most recent props the component handed to <ForceGraph2D>. The
// stub renders nothing meaningful — the test drives the captured callbacks.
let captured: Record<string, unknown> = {};
vi.mock("react-force-graph-2d", () => ({
  default: (props: Record<string, unknown>) => {
    captured = props;
    return <div data-testid="force-graph-stub" />;
  },
}));

const GRAPH: KnowledgeGraph = {
  nodes: [
    { id: "self-hosting", label: "Self-hosting", kind: "concept", weight: 2 },
    { id: "vaultwarden", label: "Vaultwarden", kind: "concept", weight: 1 },
  ],
  edges: [{ source: "self-hosting", target: "vaultwarden", type: "relates_to", weight: 0.8 }],
};

describe("ForceGraphCanvas", () => {
  beforeEach(() => {
    captured = {};
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("fires onNodeClick with the node's concept id when a node is tapped", () => {
    const onNodeClick = vi.fn();
    render(<ForceGraphCanvas graph={GRAPH} onNodeClick={onNodeClick} />);

    // The lib calls onNodeClick with the (mutated) node object — it always
    // carries the original `id`. Replay that exact call.
    const libOnNodeClick = captured.onNodeClick as (node: { id: string }) => void;
    expect(libOnNodeClick).toBeTypeOf("function");
    libOnNodeClick({ id: "self-hosting" });

    expect(onNodeClick).toHaveBeenCalledTimes(1);
    expect(onNodeClick).toHaveBeenCalledWith("self-hosting");
  });

  it("passes the node id through unchanged so it matches the inspector's concept id", () => {
    const onNodeClick = vi.fn();
    render(<ForceGraphCanvas graph={GRAPH} onNodeClick={onNodeClick} />);

    const libOnNodeClick = captured.onNodeClick as (node: { id: string }) => void;
    libOnNodeClick({ id: "vaultwarden" });

    // The id the inspector's getConceptDetail(id) expects — the graph node id,
    // verbatim (no prefixing / reshaping).
    expect(onNodeClick).toHaveBeenCalledWith("vaultwarden");
  });

  it("gives the canvas an explicit finite size so hit-detection matches the container", () => {
    render(<ForceGraphCanvas graph={GRAPH} onNodeClick={vi.fn()} />);

    // The root cause of the dead clicks: with no width/height the lib defaults
    // to window.innerWidth/innerHeight, so the shadow-canvas the click reads is
    // sized to the whole window while the visible box is clipped to ~360px —
    // the read pixel never lands on a node. Both must be finite positive numbers.
    expect(captured.width).toBeTypeOf("number");
    expect(captured.height).toBeTypeOf("number");
    expect(captured.width as number).toBeGreaterThan(0);
    expect(captured.height as number).toBeGreaterThan(0);
  });

  it("does not throw when onNodeClick is omitted", () => {
    render(<ForceGraphCanvas graph={GRAPH} />);
    const libOnNodeClick = captured.onNodeClick as (node: { id: string }) => void;
    expect(() => libOnNodeClick({ id: "self-hosting" })).not.toThrow();
  });
});
