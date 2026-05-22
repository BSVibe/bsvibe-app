/**
 * ┌──────────────────────────────────────────────────────────────────────┐
 * │  PLACEHOLDER DATA — NOT REAL.                                          │
 * │                                                                        │
 * │  The Glance surface (UX §3) needs three things the backend does not    │
 * │  yet serve: per-product run STATUS, the "needs you" decision queue,    │
 * │  and "recently shipped" deliverables. Until those endpoints exist,     │
 * │  the values below stand in. They are the ONLY non-real data in the     │
 * │  whole API client.                                                     │
 * │                                                                        │
 * │  To go live: in lib/api/brief.ts replace the three `PLACEHOLDER_*`     │
 * │  references with real apiFetch() calls (e.g. /api/v1/runs,             │
 * │  /api/v1/decisions, /api/v1/deliverables). The UI consumes the         │
 * │  BriefView shape only and needs no further change.                     │
 * │                                                                        │
 * │  The data mirrors the Stitch Brief mockup so the surface reads true.   │
 * └──────────────────────────────────────────────────────────────────────┘
 */

import type { LaneState, NeedsYouItem, ProductLane, ShippedItem } from "./types";

/** Demo product lanes — used when the real /api/v1/products list is empty. */
export const PLACEHOLDER_LANES: ProductLane[] = [
  {
    id: "ph-bsvibe-site",
    slug: "bsvibe-site",
    name: "bsvibe-site",
    state: "working",
    status: "writing tests for the related-posts feature · 4m in",
  },
  {
    id: "ph-acme-corp",
    slug: "acme-corp",
    name: "acme-corp",
    state: "needs-you",
    status: "paused — which auth approach?",
  },
  {
    id: "ph-quantum-link",
    slug: "quantum-link",
    name: "quantum-link",
    state: "triggered",
    status: "started from a GitHub issue · decomposing… · 1m in",
  },
  {
    id: "ph-stellar-app",
    slug: "stellar-app",
    name: "stellar-app",
    state: "shipped",
    status: "ingestion retry logic → PR #20 · verified",
  },
  {
    id: "ph-nexus-portal",
    slug: "nexus-portal",
    name: "nexus-portal",
    state: "idle",
    status: "—",
  },
];

/** Demo "needs you" decisions. */
export const PLACEHOLDER_NEEDS_YOU: NeedsYouItem[] = [
  {
    id: "ph-needs-1",
    productSlug: "bsvibe-site",
    question: "keep only the 4 i18n keys, or rewrite the direction?",
  },
  {
    id: "ph-needs-2",
    productSlug: "acme-corp",
    question: "which auth approach?",
  },
];

/** Demo "recently shipped" deliverables, mixed artifact types (UX §4). */
export const PLACEHOLDER_RECENTLY_SHIPPED: ShippedItem[] = [
  {
    id: "ph-ship-1",
    title: "getRelatedPosts function",
    productSlug: "bsvibe-site",
    source: "GitHub PR #15",
    artifactType: "pr",
    verdict: "This is verified",
  },
  {
    id: "ph-ship-2",
    title: "Q3 launch plan v2",
    productSlug: "acme-corp",
    source: "Notion page",
    artifactType: "doc",
    verdict: "This is verified",
  },
  {
    id: "ph-ship-3",
    title: "Hero illustration",
    productSlug: "stellar-app",
    source: "Figma frame",
    artifactType: "image",
    verdict: "This is verified",
  },
];

/** Plain-language placeholder status for a REAL product with no run data yet. */
export function placeholderLaneStatus(state: LaneState): string {
  switch (state) {
    case "working":
      return "working on your latest direction";
    case "needs-you":
      return "paused — needs a call from you";
    case "triggered":
      return "just started · decomposing…";
    case "shipped":
      return "shipped · verified";
    default:
      return "—";
  }
}
