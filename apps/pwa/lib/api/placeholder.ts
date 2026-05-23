/**
 * ┌──────────────────────────────────────────────────────────────────────┐
 * │  PLACEHOLDER DATA — NOT REAL.                                          │
 * │                                                                        │
 * │  The Brief now reads REAL data (lib/api/brief.ts): product lanes from  │
 * │  /api/v1/products + /api/v1/runs, "Needs you" from /api/v1/decisions   │
 * │  + /api/v1/safemode/queue, "Recently shipped" from /api/v1/runs.       │
 * │                                                                        │
 * │  The ONLY thing left here is a demo set of product lanes, used purely  │
 * │  as a FALLBACK when the network / auth fails mid-load — so the surface │
 * │  shows a calm board instead of an error wall. It is never shown on a   │
 * │  successful (even empty) read; an empty workspace renders calm empty   │
 * │  states from the real data, not this.                                  │
 * │                                                                        │
 * │  The remaining genuine gap (no endpoint yet) is the shipped-item       │
 * │  title/source detail — there is no deliverable-read endpoint, only     │
 * │  runs — so brief.ts derives that from the run and keeps                │
 * │  BriefView.placeholder true while any shipped item is shown.           │
 * └──────────────────────────────────────────────────────────────────────┘
 */

import type { ProductLane } from "./types";

/** Demo product lanes — shown ONLY when the real reads fail mid-load. */
export const PLACEHOLDER_LANES: ProductLane[] = [
  {
    id: "ph-bsvibe-site",
    slug: "bsvibe-site",
    name: "bsvibe-site",
    state: "working",
    status: "working on your latest direction",
  },
  {
    id: "ph-acme-corp",
    slug: "acme-corp",
    name: "acme-corp",
    state: "needs-you",
    status: "paused — needs a call from you",
  },
  {
    id: "ph-stellar-app",
    slug: "stellar-app",
    name: "stellar-app",
    state: "shipped",
    status: "shipped · verified",
  },
];
