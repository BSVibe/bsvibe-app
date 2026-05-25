"use client";

import { ApiError } from "@/lib/api/client";
import { getConceptDetail } from "@/lib/api/knowledge";
import type { ConceptDetail, KnowledgeGraph } from "@/lib/api/types";
import {
  type GraphLink,
  computeDegree,
  labelDegreeThreshold,
  nodeRadius,
  shouldShowLabel,
} from "@/lib/knowledge/graphPhysics";
import { forceCollide, forceX, forceY } from "d3-force";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D from "react-force-graph-2d";

/**
 * The Knowledge graph view — ported from BSage's `KnowledgeGraphView` and
 * adapted to the monorepo's `/api/v1/inside` data. This is the WHOLE knowledge
 * surface: a force-directed graph (left) + a click→detail side panel (right),
 * plus a search box and a group(kind) color legend/filter.
 *
 * WHY THIS REPLACES `ForceGraphCanvas`: the old canvas used a custom
 * `nodeCanvasObject` but provided NO `nodePointerAreaPaint`, so the lib's click
 * hit-area (a shadow canvas read under the pointer) never matched the drawn
 * nodes — every real tap resolved to nothing and `onNodeClick` never fired.
 * BSage's component supplies `nodePointerAreaPaint` (a generous circular hit
 * area per node), which is what makes clicks work. We keep that verbatim.
 *
 * Loaded only on the client (`next/dynamic` with `ssr: false` from the parent)
 * because `react-force-graph-2d` reaches for `window`/canvas at module scope.
 *
 * Dropped from BSage's version (no equivalent data in the monorepo): the
 * community color-mode + community legend (no community data on `/inside`), and
 * the markdown/wikilink note rendering (no react-markdown/remark/rehype dep) —
 * the detail panel renders aliases + related concepts + source observations as
 * plain styled elements instead.
 */

// Palette applied in deterministic order to whatever kinds show up in the
// graph. A new ontology kind lands on a fresh slot with no frontend change.
// Tuned to read on the calm light/dark surfaces (mid-saturation, mid-value).
const GROUP_PALETTE = [
  "#5b8def",
  "#3fb68b",
  "#e08a3c",
  "#c2569e",
  "#4aa3c7",
  "#8b6fd6",
  "#cf5b5b",
  "#5aa469",
  "#caa23a",
  "#6d7dca",
];
const FALLBACK_COLOR = "#8b6fd6";

/** The empty/unknown kind gets the translated "Other" label; everything else is
 *  humanized from the backend id. */
function humanizeGroup(group: string): string | null {
  if (!group) return null;
  const cleaned = group.replace(/^_/, "").replace(/[-_]/g, " ");
  return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
}

interface GraphNode {
  id: string;
  name: string;
  group: string;
  weight: number;
  x?: number;
  y?: number;
}

type DetailState =
  | { status: "idle" }
  | { status: "loading"; id: string; name: string }
  | { status: "not-found"; id: string; name: string }
  | { status: "error"; id: string; name: string }
  | { status: "ready"; detail: ConceptDetail };

interface Palette {
  edge: string;
  label: string;
  labelSelected: string;
  ring: string;
}

function readPalette(): Palette {
  if (typeof window === "undefined") {
    return { edge: "#e3e1db", label: "#6b6a66", labelSelected: "#37352f", ring: "#37352f" };
  }
  const styles = getComputedStyle(document.documentElement);
  const token = (name: string, fallback: string) =>
    styles.getPropertyValue(name).trim() || fallback;
  return {
    edge: token("--hair-strong", "#e3e1db"),
    label: token("--ink-2", "#6b6a66"),
    labelSelected: token("--ink", "#37352f"),
    ring: token("--ink", "#37352f"),
  };
}

export default function KnowledgeGraphView({ graph }: { graph: KnowledgeGraph }) {
  const t = useTranslations("knowledge");
  const [searchQuery, setSearchQuery] = useState("");
  // `null` = no filter (all kinds visible). First toggle seeds the allowlist to
  // every known kind, then toggles the clicked one off.
  const [activeFilters, setActiveFilters] = useState<Set<string> | null>(null);
  const [detail, setDetail] = useState<DetailState>({ status: "idle" });
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const containerRef = useRef<HTMLDivElement>(null);
  // biome-ignore lint/suspicious/noExplicitAny: the lib's ref is untyped.
  const fgRef = useRef<any>(null);
  const [dimensions, setDimensions] = useState<{ width: number; height: number }>({
    width: 640,
    height: 420,
  });
  const [palette, setPalette] = useState<Palette>(() => readPalette());

  // Measure the host box and feed its size to the lib. Without explicit
  // width/height the lib defaults to window.innerWidth/innerHeight, mismatching
  // the clipped host AND breaking the shadow-canvas hit-test.
  useEffect(() => {
    const el = containerRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const measure = () => {
      const { width, height } = el.getBoundingClientRect();
      if (width > 0 && height > 0) setDimensions({ width, height });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
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

  // Map the `/inside/graph` shape onto the lib's node/link shape once per
  // response: label→name, kind→group (color/filter), weight kept for sizing.
  const baseNodes = useMemo<GraphNode[]>(
    () =>
      graph.nodes.map((n) => ({
        id: n.id,
        name: n.label,
        group: n.kind ?? "",
        weight: n.weight,
      })),
    [graph],
  );

  // Legend entries derived from actual data: each unique kind gets a color
  // (palette cycles by frequency rank) and a node count.
  const groupsInfo = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const n of baseNodes) counts[n.group] = (counts[n.group] ?? 0) + 1;
    return Object.entries(counts)
      .sort(([, a], [, b]) => b - a)
      .map(([group, count], idx) => ({
        group,
        count,
        label: humanizeGroup(group) ?? t("graphGroupOther"),
        color: GROUP_PALETTE[idx % GROUP_PALETTE.length],
      }));
  }, [baseNodes, t]);

  const groupColorMap = useMemo(() => {
    const map: Record<string, string> = {};
    for (const g of groupsInfo) map[g.group] = g.color;
    return map;
  }, [groupsInfo]);

  const toggleFilter = useCallback(
    (group: string) => {
      setActiveFilters((prev) => {
        const base = prev ?? new Set(groupsInfo.map((g) => g.group));
        const next = new Set(base);
        if (next.has(group)) next.delete(group);
        else next.add(group);
        return next;
      });
    },
    [groupsInfo],
  );

  // Filtered nodes + edges (search needle + kind allowlist). Edges survive only
  // when both endpoints survive. New node/link objects each pass so the sim
  // re-seeds cleanly.
  const filteredData = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    const nodes = baseNodes.filter((n) => {
      if (activeFilters && !activeFilters.has(n.group)) return false;
      if (query && !n.name.toLowerCase().includes(query)) return false;
      return true;
    });
    const nodeIds = new Set(nodes.map((n) => n.id));
    const links = graph.edges
      .filter((e) => nodeIds.has(e.source) && nodeIds.has(e.target))
      .map((e) => ({ source: e.source, target: e.target }));
    return { nodes: nodes.map((n) => ({ ...n })), links };
  }, [baseNodes, graph.edges, activeFilters, searchQuery]);

  const degreeMap = useMemo(() => computeDegree(filteredData.links as GraphLink[]), [filteredData]);
  const hubThreshold = useMemo(() => labelDegreeThreshold(degreeMap, 0.05), [degreeMap]);

  // Tune the built-in forces in place (replacing the link force breaks the
  // lib's id-resolution) + add collide/centering for a calmer layout.
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg?.d3Force) return;
    const charge = fg.d3Force("charge");
    if (charge?.strength) charge.strength(-160);
    const link = fg.d3Force("link");
    if (link?.distance) {
      link.distance(44);
      link.strength?.(0.4);
    }
    fg.d3Force(
      "collide",
      forceCollide()
        .radius((d: unknown) => {
          const id = (d as { id?: string }).id;
          return nodeRadius((id && degreeMap[id]) || 0, false) + 8;
        })
        .iterations(1),
    );
    // Weak centering keeps isolated/low-degree nodes inside the canvas.
    fg.d3Force("x", forceX(0).strength(0.05));
    fg.d3Force("y", forceY(0).strength(0.05));
  }, [degreeMap]);

  const handleNodeClick = useCallback(async (node: { id?: string; name?: string }) => {
    if (!node.id) return;
    const id = node.id;
    const name = node.name ?? id;
    setSelectedId(id);
    setDetail({ status: "loading", id, name });
    try {
      const d = await getConceptDetail(id);
      setDetail({ status: "ready", detail: d });
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setDetail({ status: "not-found", id, name });
      } else {
        setDetail({ status: "error", id, name });
      }
    }
  }, []);

  const closePanel = useCallback(() => {
    setSelectedId(null);
    setDetail({ status: "idle" });
  }, []);

  const nodeCanvasObject = useCallback(
    (rawNode: GraphNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const x = rawNode.x ?? 0;
      const y = rawNode.y ?? 0;
      const label = rawNode.name || "";
      const color = groupColorMap[rawNode.group] ?? FALLBACK_COLOR;
      const isSelected = selectedId === rawNode.id;
      const degree = (rawNode.id && degreeMap[rawNode.id]) || 0;
      const radius = nodeRadius(degree, isSelected);

      // Glow + dashed orbit for the selected node.
      if (isSelected) {
        ctx.beginPath();
        ctx.arc(x, y, radius + 10, 0, 2 * Math.PI);
        const gradient = ctx.createRadialGradient(x, y, radius, x, y, radius + 10);
        gradient.addColorStop(0, `${color}40`);
        gradient.addColorStop(1, `${color}00`);
        ctx.fillStyle = gradient;
        ctx.fill();

        ctx.beginPath();
        ctx.arc(x, y, radius + 6, 0, 2 * Math.PI);
        ctx.strokeStyle = `${color}60`;
        ctx.lineWidth = 1 / globalScale;
        ctx.setLineDash([4 / globalScale, 2 / globalScale]);
        ctx.stroke();
        ctx.setLineDash([]);
      }

      // Node circle.
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.fill();

      if (isSelected) {
        ctx.strokeStyle = palette.ring;
        ctx.lineWidth = 2 / globalScale;
        ctx.stroke();
      }

      // LOD label — only at zoom OR for hub/selected nodes.
      if (!isSelected && !shouldShowLabel(globalScale, degree, hubThreshold)) return;
      const fontSize = Math.max(12 / globalScale, 3);
      ctx.font = `${fontSize}px ui-sans-serif, system-ui, sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillStyle = isSelected ? palette.labelSelected : palette.label;
      ctx.fillText(label, x, y + radius + 2);
    },
    [selectedId, groupColorMap, degreeMap, hubThreshold, palette],
  );

  // The click hit-area — the fix for the dead clicks. A generous circle (min
  // 12px) per node so even isolated/low-degree nodes stay tappable at default
  // zoom. WITHOUT this, the lib has no hit-area matching the custom-drawn nodes
  // and onNodeClick never fires.
  const nodePointerAreaPaint = useCallback(
    (node: GraphNode, color: string, ctx: CanvasRenderingContext2D) => {
      const deg = (node.id && degreeMap[node.id]) || 0;
      const r = Math.max(12, nodeRadius(deg, false) + 6);
      ctx.beginPath();
      ctx.arc(node.x ?? 0, node.y ?? 0, r, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.fill();
    },
    [degreeMap],
  );

  return (
    <div className="kgraph">
      <div className="kgraph__main">
        {/* Top bar: search + reset-filters */}
        <div className="kgraph__toolbar">
          <input
            type="search"
            className="kgraph__search"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder={t("searchPlaceholder")}
            aria-label={t("searchLabel")}
          />
          {activeFilters && (
            <button
              type="button"
              className="kgraph__show-all"
              onClick={() => setActiveFilters(null)}
            >
              {t("graphShowAll")}
            </button>
          )}
        </div>

        {/* Canvas host — measured + sized so clicks land. */}
        <div ref={containerRef} className="kgraph__canvas" data-testid="knowledge-graph-canvas">
          {filteredData.nodes.length === 0 ? (
            <div className="kgraph__no-match">
              <p>{searchQuery || activeFilters ? t("graphNoMatch") : t("graphEmptyLine")}</p>
            </div>
          ) : (
            <ForceGraph2D
              ref={fgRef}
              graphData={filteredData}
              width={dimensions.width}
              height={dimensions.height}
              nodeId="id"
              nodeLabel="name"
              backgroundColor="rgba(0,0,0,0)"
              linkColor={() => palette.edge}
              linkWidth={1}
              cooldownTicks={120}
              warmupTicks={40}
              d3VelocityDecay={0.4}
              enableNodeDrag
              enableZoomInteraction
              enablePanInteraction
              onNodeClick={handleNodeClick}
              nodeCanvasObject={nodeCanvasObject}
              nodePointerAreaPaint={nodePointerAreaPaint}
            />
          )}

          {/* Group(kind) legend + filter. */}
          {groupsInfo.length > 0 && (
            <div className="kgraph__legend" aria-label={t("graphLegendLabel")}>
              {groupsInfo.map(({ group, count, label, color }) => {
                const active = activeFilters === null || activeFilters.has(group);
                return (
                  <button
                    key={group || "__other"}
                    type="button"
                    className={`kgraph__legend-item${active ? "" : " kgraph__legend-item--off"}`}
                    onClick={() => toggleFilter(group)}
                    aria-pressed={active}
                  >
                    <span className="kgraph__legend-dot" style={{ backgroundColor: color }} />
                    <span className="kgraph__legend-label">{label}</span>
                    <span className="kgraph__legend-count">{count}</span>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {/* Detail side panel — opens on a node tap. */}
      {detail.status !== "idle" && (
        <aside className="kgraph__panel" aria-label={t("inspectorLabel")}>
          <header className="kgraph__panel-head">
            <h2 className="kgraph__panel-name">
              {detail.status === "ready" ? detail.detail.name : detail.name}
            </h2>
            <button
              type="button"
              className="kgraph__panel-close"
              onClick={closePanel}
              aria-label={t("inspectorClose")}
            >
              ×
            </button>
          </header>

          {detail.status === "loading" && (
            <p className="kgraph__panel-note" aria-live="polite">
              {t("inspectorLoading")}
            </p>
          )}
          {detail.status === "not-found" && (
            <p className="kgraph__panel-note" aria-live="polite">
              {t("inspectorNotFound")}
            </p>
          )}
          {detail.status === "error" && (
            <p className="kgraph__panel-note" aria-live="polite">
              {t("inspectorError")}
            </p>
          )}

          {detail.status === "ready" && (
            <div className="kgraph__panel-body">
              {detail.detail.aliases.length > 0 && (
                <section className="kgraph__block">
                  <h3 className="kgraph__block-label">{t("inspectorAliases")}</h3>
                  <ul className="kgraph__tags">
                    {detail.detail.aliases.map((alias) => (
                      <li key={alias} className="kgraph__tag">
                        {alias}
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              <section className="kgraph__block">
                <h3 className="kgraph__block-label">{t("inspectorRelated")}</h3>
                {detail.detail.related.length === 0 ? (
                  <p className="kgraph__empty">{t("inspectorRelatedEmpty")}</p>
                ) : (
                  <ul className="kgraph__related">
                    {detail.detail.related.map((rel) => (
                      <li key={rel.id}>
                        <button
                          type="button"
                          className="kgraph__chip"
                          onClick={() => handleNodeClick({ id: rel.id, name: rel.name })}
                        >
                          <span className="kgraph__chip-name">{rel.name}</span>
                          <span className="kgraph__chip-weight">
                            {t("relatedWeight", { count: rel.weight })}
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              <section className="kgraph__block">
                <h3 className="kgraph__block-label">{t("inspectorOrigin")}</h3>
                {detail.detail.observations.length === 0 ? (
                  <p className="kgraph__empty">{t("inspectorOriginEmpty")}</p>
                ) : (
                  <ul className="kgraph__obs">
                    {detail.detail.observations.map((obs) => (
                      <li key={obs.id} className="kgraph__obs-row">
                        <div className="kgraph__obs-head">
                          <span className="kgraph__obs-title">{obs.title}</span>
                          {obs.captured_at && (
                            <span className="kgraph__obs-date">{obs.captured_at}</span>
                          )}
                        </div>
                        {obs.excerpt && <p className="kgraph__obs-excerpt">{obs.excerpt}</p>}
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            </div>
          )}
        </aside>
      )}
    </div>
  );
}
