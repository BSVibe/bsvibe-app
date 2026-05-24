"use client";

import type { KnowledgeGraph } from "@/lib/api/types";
import { useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D from "react-force-graph-2d";

/**
 * The force-directed canvas itself — `react-force-graph-2d` over canvas/WebGL.
 * Imported ONLY on the client (the parent loads it via `next/dynamic` with
 * `ssr: false`) because the lib reaches for `window`/canvas at module scope and
 * would break SSR + the static build otherwise.
 *
 * Colours are read from the shared CSS tokens (`--ink`, `--ink-3`, `--accent`,
 * `--hair-strong`, `--surface`) resolved from the live `data-theme`, so the
 * canvas stays legible in both light and dark without a second palette. The
 * graph is pannable + zoomable (the lib's default interaction), nodes are
 * labelled, and edges are drawn between them. We only tune the built-in `link`
 * / `charge` forces (never replace them — replacing the link force breaks the
 * lib's id-resolution and the canvas stops repainting).
 *
 * CRITICAL: `react-force-graph-2d` does NOT auto-size — given no `width`/
 * `height` it falls back to `window.innerWidth`/`innerHeight`. The host box is
 * clipped to a fixed height (`.knowledge-graph__canvas`, `overflow: hidden`),
 * so a window-sized canvas centres the simulation far below the visible slice
 * AND, worse, makes the lib's shadow-canvas hit-detection (it reads the pixel
 * under the pointer, mapped relative to the container's top-left) read a region
 * that no longer holds the visible nodes — every node tap resolves to nothing,
 * so `onNodeClick` never fires and the inspector never opens. We measure the
 * host box (ResizeObserver) and pass explicit `width`/`height` so the canvas
 * coordinate space matches what the founder sees and clicks register.
 */

interface Palette {
  node: string;
  edge: string;
  text: string;
  /** Muted tone for nodes/labels that fall outside the active search filter. */
  dim: string;
}

function readPalette(): Palette {
  if (typeof window === "undefined") {
    return { node: "#6b6a66", edge: "#e3e1db", text: "#37352f", dim: "#c9c7c1" };
  }
  const styles = getComputedStyle(document.documentElement);
  const token = (name: string, fallback: string) =>
    styles.getPropertyValue(name).trim() || fallback;
  return {
    node: token("--ink-2", "#6b6a66"),
    edge: token("--hair-strong", "#e3e1db"),
    text: token("--ink", "#37352f"),
    dim: token("--ink-3", "#c9c7c1"),
  };
}

interface ForceNode {
  id: string;
  label: string;
  val: number;
}

interface ForceLink {
  source: string;
  target: string;
}

export default function ForceGraphCanvas({
  graph,
  filter = "",
  onNodeClick,
}: {
  graph: KnowledgeGraph;
  /** Search needle — matching nodes stay vivid, non-matching nodes dim. */
  filter?: string;
  /** Fired with a node id when a node is tapped (opens the inspector). */
  onNodeClick?: (id: string) => void;
}) {
  const needle = filter.trim().toLowerCase();
  const isMatch = (label: string) => needle === "" || label.toLowerCase().includes(needle);
  // The lib's d3 sim resolves string link.source/target against node ids, so we
  // pass the raw ids and let the built-in link force do the resolution.
  const data = useMemo(
    () => ({
      nodes: graph.nodes.map<ForceNode>((n) => ({
        id: n.id,
        label: n.label,
        // Node area scales (gently) with degree so hubs read as bigger.
        val: 1 + n.weight,
      })),
      links: graph.edges.map<ForceLink>((e) => ({ source: e.source, target: e.target })),
    }),
    [graph],
  );

  const [palette, setPalette] = useState<Palette>(() => readPalette());

  // Measure the host box and feed its size to the lib. Without this the lib
  // defaults to window.innerWidth/innerHeight, mismatching the clipped host —
  // which both hides most nodes and breaks the shadow-canvas click hit-test
  // (see the file header). Default to the CSS box height (360px) so the first
  // paint — before the observer fires — is already roughly correct.
  const hostRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState<{ width: number; height: number }>({
    width: 640,
    height: 360,
  });
  useEffect(() => {
    const host = hostRef.current;
    if (!host || typeof ResizeObserver === "undefined") return;
    const measure = () => {
      const { width, height } = host.getBoundingClientRect();
      // Guard against a 0×0 (pre-layout) read so the canvas never collapses.
      if (width > 0 && height > 0) setSize({ width, height });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  // Re-read tokens when the theme flips (data-theme on <html>).
  useEffect(() => {
    if (typeof window === "undefined") return;
    const update = () => setPalette(readPalette());
    update();
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    return () => observer.disconnect();
  }, []);

  // Modify (never replace) the built-in forces for a calmer, tighter layout.
  // biome-ignore lint/suspicious/noExplicitAny: the lib's ref is untyped.
  const fgRef = useRef<any>(null);
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;
    const charge = fg.d3Force("charge");
    if (charge?.strength) charge.strength(-120);
    const link = fg.d3Force("link");
    if (link?.distance) link.distance(48);
  }, []);

  return (
    // The host fills the CSS box (`.knowledge-graph__canvas`); we measure it and
    // size the canvas to match so clicks land on nodes (see the file header).
    <div ref={hostRef} style={{ width: "100%", height: "100%" }}>
      <ForceGraph2D
        ref={fgRef}
        width={size.width}
        height={size.height}
        graphData={data}
        nodeId="id"
        nodeLabel="label"
        nodeVal="val"
        nodeColor={(node: ForceNode) => (isMatch(node.label) ? palette.node : palette.dim)}
        linkColor={() => palette.edge}
        linkWidth={1}
        backgroundColor="rgba(0,0,0,0)"
        enableZoomInteraction
        enablePanInteraction
        cooldownTicks={120}
        warmupTicks={40}
        onNodeClick={(node: ForceNode) => onNodeClick?.(node.id)}
        // Draw the node label beneath each node so the graph reads as knowledge,
        // not anonymous dots.
        nodeCanvasObjectMode={() => "after"}
        nodeCanvasObject={(node: ForceNode & { x?: number; y?: number }, ctx, globalScale) => {
          if (node.x === undefined || node.y === undefined) return;
          const fontSize = 11 / globalScale;
          ctx.font = `${fontSize}px ui-sans-serif, system-ui, sans-serif`;
          ctx.textAlign = "center";
          ctx.textBaseline = "top";
          ctx.fillStyle = isMatch(node.label) ? palette.text : palette.dim;
          ctx.fillText(node.label, node.x, node.y + 5 / globalScale);
        }}
      />
    </div>
  );
}
