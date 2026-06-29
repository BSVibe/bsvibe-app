/**
 * Local-graph transform (Lift 5 — KG redesign).
 *
 * The global knowledge graph is concept-only and clean, but at 437 nodes it is
 * a hairball — past ~500 nodes a force-directed view has no navigability (the
 * digital-garden research is explicit: the answer is a per-concept *local*
 * graph, the focused note ± its neighbours, independent of vault size). This
 * builds exactly that, purely from the `ConceptDetail` the inspector already
 * fetches — so it needs NO backend change:
 *
 *   focus concept ──related──▶ related concept   (1-hop concept neighbours)
 *                 ──member───▶ seedling           (the observations it distils)
 *
 * Concepts stay first-class nodes; the member observations surface as seedling
 * leaves so "the content lives in the leaves" is finally visible around its
 * hub. Kept pure (no React, no canvas) so it is unit-testable on its own.
 */

import type { ConceptDetail } from "@/lib/api/types";

export interface LocalGraphNode {
  id: string;
  name: string;
  /** Concepts are hubs; seedlings are the member-observation leaves. */
  nodeType: "concept" | "seedling";
  /** The inspected concept at the centre of this local view. */
  focus: boolean;
  /** Ontology kind of the focus concept (for TYPE colouring); "" otherwise. */
  group: string;
  /** Emergent community of the focus concept; "" otherwise. */
  community: string;
  /** Connectedness signal for node sizing. */
  weight: number;
}

export interface LocalGraphLink {
  source: string;
  target: string;
  type: "related" | "member";
}

export interface LocalGraph {
  nodes: LocalGraphNode[];
  links: LocalGraphLink[];
}

/**
 * Build the 1-hop local graph around one inspected concept.
 *
 * `focusMeta` carries the focus concept's TYPE/community from the global graph
 * node (the `ConceptDetail` doesn't repeat `community`), so the centre node
 * colours consistently with the overview. Node ids are de-duplicated with the
 * focus winning — a self-referential `related` entry never spawns a second
 * node.
 */
export function buildLocalGraph(
  detail: ConceptDetail,
  focusMeta?: { group?: string | null; community?: string | null } | null,
): LocalGraph {
  const nodes: LocalGraphNode[] = [];
  const links: LocalGraphLink[] = [];
  const seen = new Set<string>();

  const focusId = detail.id;
  nodes.push({
    id: focusId,
    name: detail.name,
    nodeType: "concept",
    focus: true,
    group: focusMeta?.group ?? detail.type ?? "",
    community: focusMeta?.community ?? "",
    weight: detail.related.length + detail.observations.length,
  });
  seen.add(focusId);

  for (const rel of detail.related) {
    if (seen.has(rel.id)) continue;
    seen.add(rel.id);
    nodes.push({
      id: rel.id,
      name: rel.name,
      nodeType: "concept",
      focus: false,
      group: "",
      community: "",
      weight: rel.weight,
    });
    links.push({ source: focusId, target: rel.id, type: "related" });
  }

  for (const obs of detail.observations) {
    if (seen.has(obs.id)) continue;
    seen.add(obs.id);
    nodes.push({
      id: obs.id,
      name: obs.title || obs.id,
      nodeType: "seedling",
      focus: false,
      group: "",
      community: "",
      weight: 1,
    });
    links.push({ source: focusId, target: obs.id, type: "member" });
  }

  return { nodes, links };
}
