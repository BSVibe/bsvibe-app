/**
 * `buildLocalGraph` — Lift 5 (KG redesign). The deterministic transform behind
 * the local-graph view: a focused concept's 1-hop neighbourhood (the focus
 * concept + its related concepts + its member observations as seedling leaves)
 * built purely from the `ConceptDetail` the inspector already fetches. No
 * backend change — this is the data the global hairball can't show navigably.
 *
 * The research is explicit: a 437-node global graph is unusable (>500 = no
 * navigability); the answer is a per-concept local graph. This module is that
 * graph's shape, kept pure so it is unit-testable away from the canvas.
 */

import type { ConceptDetail } from "@/lib/api/types";
import { buildLocalGraph } from "@/lib/knowledge/localGraph";
import { describe, expect, it } from "vitest";

function detail(overrides: Partial<ConceptDetail> = {}): ConceptDetail {
  return {
    id: "auth",
    name: "Auth",
    aliases: [],
    related: [
      { id: "jwks", name: "JWKS", weight: 3 },
      { id: "session", name: "Session", weight: 1 },
    ],
    observations: [
      {
        id: "garden/seedling/auth-callback.md",
        title: "Wired the auth callback",
        excerpt: "redirect target confirmed",
        body: "redirect target confirmed",
        truncated: false,
        captured_at: "2026-05-21",
      },
    ],
    type: "Pattern",
    ...overrides,
  };
}

describe("buildLocalGraph", () => {
  it("puts the inspected concept at the centre as the focus node", () => {
    const { nodes } = buildLocalGraph(detail());
    const focus = nodes.find((n) => n.id === "auth");
    expect(focus).toBeDefined();
    expect(focus?.focus).toBe(true);
    expect(focus?.nodeType).toBe("concept");
    expect(focus?.name).toBe("Auth");
  });

  it("adds each related concept as a 1-hop concept node linked to the focus", () => {
    const { nodes, links } = buildLocalGraph(detail());
    const jwks = nodes.find((n) => n.id === "jwks");
    expect(jwks).toMatchObject({ nodeType: "concept", focus: false });
    // A 'related' edge from focus to each neighbour.
    const related = links.filter((l) => l.type === "related");
    expect(related).toHaveLength(2);
    expect(related.every((l) => l.source === "auth")).toBe(true);
    expect(new Set(related.map((l) => l.target))).toEqual(new Set(["jwks", "session"]));
  });

  it("adds each member observation as a seedling leaf linked by a member edge", () => {
    const { nodes, links } = buildLocalGraph(detail());
    const seed = nodes.find((n) => n.id === "garden/seedling/auth-callback.md");
    expect(seed).toMatchObject({ nodeType: "seedling", focus: false });
    // Seedling label is the observation title, not the raw path.
    expect(seed?.name).toBe("Wired the auth callback");
    const member = links.filter((l) => l.type === "member");
    expect(member).toEqual([
      { source: "auth", target: "garden/seedling/auth-callback.md", type: "member" },
    ]);
  });

  it("carries the focus concept's group/community metadata for colouring", () => {
    const { nodes } = buildLocalGraph(detail(), { group: "Pattern", community: "auth-domain" });
    const focus = nodes.find((n) => n.id === "auth");
    expect(focus?.group).toBe("Pattern");
    expect(focus?.community).toBe("auth-domain");
  });

  it("never duplicates a node id (a related id colliding with the focus is dropped)", () => {
    const { nodes } = buildLocalGraph(
      detail({ related: [{ id: "auth", name: "Auth (self)", weight: 1 }] }),
    );
    expect(nodes.filter((n) => n.id === "auth")).toHaveLength(1);
    expect(nodes.find((n) => n.id === "auth")?.focus).toBe(true);
  });

  it("a lone concept (no related, no observations) yields just the focus node", () => {
    const { nodes, links } = buildLocalGraph(detail({ related: [], observations: [] }));
    expect(nodes).toHaveLength(1);
    expect(nodes[0].id).toBe("auth");
    expect(links).toHaveLength(0);
  });
});
