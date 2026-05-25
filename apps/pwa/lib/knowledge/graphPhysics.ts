/**
 * Pure helpers for the knowledge graph view — ported from BSage's
 * `frontend/src/lib/graphPhysics.ts` and adapted to the monorepo's graph data
 * (`/api/v1/inside/graph` edges, no community data).
 *
 * Keeping the physics math out of the React component leaves each piece
 * independently testable and the canvas render path readable.
 */

/** One graph edge as the d3 simulation sees it: `source`/`target` are node ids
 *  before the sim resolves them, or `{ id }` node objects after. */
export interface GraphLink {
  source: string | { id: string };
  target: string | { id: string };
}

function endpointId(end: string | { id: string }): string {
  return typeof end === "string" ? end : end.id;
}

/** node id → degree (count of incident links). */
export function computeDegree(links: readonly GraphLink[]): Record<string, number> {
  const degree: Record<string, number> = {};
  for (const l of links) {
    const s = endpointId(l.source);
    const t = endpointId(l.target);
    degree[s] = (degree[s] ?? 0) + 1;
    degree[t] = (degree[t] ?? 0) + 1;
  }
  return degree;
}

/** Hub-aware node radius: log-scaled by degree. Selected nodes read larger. */
export function nodeRadius(degree: number, isSelected: boolean): number {
  if (isSelected) return 7;
  return 4 + Math.min(8, Math.log2(degree + 1));
}

/** Degree threshold below which labels are hidden at low zoom (top-percentile
 *  cutoff) — keeps the canvas calm by labelling only hubs until you zoom in. */
export function labelDegreeThreshold(
  degreeMap: Record<string, number>,
  topPercentile = 0.05,
): number {
  const values = Object.values(degreeMap);
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => b - a);
  const cutoffIdx = Math.max(0, Math.floor(sorted.length * topPercentile) - 1);
  return sorted[cutoffIdx] ?? 0;
}

/**
 * LOD label visibility — show when zoomed in OR for hub nodes.
 *
 * @param globalScale current zoom level from ForceGraph2D
 * @param degree node degree
 * @param hubThreshold degree at-or-above which a node is a hub
 * @param zoomThreshold zoom above which all labels render
 */
export function shouldShowLabel(
  globalScale: number,
  degree: number,
  hubThreshold: number,
  zoomThreshold = 1.2,
): boolean {
  if (globalScale >= zoomThreshold) return true;
  return degree >= hubThreshold && hubThreshold > 0;
}
