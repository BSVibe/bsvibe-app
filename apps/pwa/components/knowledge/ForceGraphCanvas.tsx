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
 */

interface Palette {
  node: string;
  edge: string;
  text: string;
}

function readPalette(): Palette {
  if (typeof window === "undefined") {
    return { node: "#6b6a66", edge: "#e3e1db", text: "#37352f" };
  }
  const styles = getComputedStyle(document.documentElement);
  const token = (name: string, fallback: string) =>
    styles.getPropertyValue(name).trim() || fallback;
  return {
    node: token("--ink-2", "#6b6a66"),
    edge: token("--hair-strong", "#e3e1db"),
    text: token("--ink", "#37352f"),
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

export default function ForceGraphCanvas({ graph }: { graph: KnowledgeGraph }) {
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
    <ForceGraph2D
      ref={fgRef}
      graphData={data}
      nodeId="id"
      nodeLabel="label"
      nodeVal="val"
      nodeColor={() => palette.node}
      linkColor={() => palette.edge}
      linkWidth={1}
      backgroundColor="rgba(0,0,0,0)"
      enableZoomInteraction
      enablePanInteraction
      cooldownTicks={120}
      warmupTicks={40}
      // Draw the node label beneath each node so the graph reads as knowledge,
      // not anonymous dots.
      nodeCanvasObjectMode={() => "after"}
      nodeCanvasObject={(node: ForceNode & { x?: number; y?: number }, ctx, globalScale) => {
        if (node.x === undefined || node.y === undefined) return;
        const fontSize = 11 / globalScale;
        ctx.font = `${fontSize}px ui-sans-serif, system-ui, sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillStyle = palette.text;
        ctx.fillText(node.label, node.x, node.y + 5 / globalScale);
      }}
    />
  );
}
