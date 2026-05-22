/**
 * Composes the Brief (Glance) view-model from the API.
 *
 * REAL today: the product list (`/api/v1/products`). For each real product we
 * render a calm status lane, but the *status text itself* is still placeholder
 * (no run-status endpoint yet). When the product list is empty (a fresh
 * workspace) we fall back to the demo lanes so the surface reads true.
 *
 * PLACEHOLDER today: per-product run status, the "needs you" queue, and
 * "recently shipped". See lib/api/placeholder.ts for the one-line swap to go
 * live. `BriefView.placeholder` stays true while any of these is demo data.
 */

import { ApiError } from "./client";
import {
  PLACEHOLDER_LANES,
  PLACEHOLDER_NEEDS_YOU,
  PLACEHOLDER_RECENTLY_SHIPPED,
  placeholderLaneStatus,
} from "./placeholder";
import { listProducts } from "./products";
import type { BriefView, ProductLane } from "./types";

function laneFromProduct(name: string, slug: string, id: string): ProductLane {
  // PLACEHOLDER: real products have no run state yet; show a calm "idle"-ish
  // working lane. Swap `state` for the real run status when it lands.
  const state = "working" as const;
  return { id, slug, name, state, status: placeholderLaneStatus(state) };
}

export async function getBrief(): Promise<BriefView> {
  let lanes: ProductLane[];
  try {
    const products = await listProducts();
    lanes = products.length
      ? products.map((p) => laneFromProduct(p.name, p.slug, p.id))
      : PLACEHOLDER_LANES;
  } catch (error) {
    // No backend / not authed mid-load → show the demo lanes rather than an
    // error wall. A real 4xx still surfaces the demo (the gate handles 401).
    if (!(error instanceof ApiError) && !(error instanceof TypeError)) throw error;
    lanes = PLACEHOLDER_LANES;
  }

  return {
    needsYou: PLACEHOLDER_NEEDS_YOU,
    lanes,
    recentlyShipped: PLACEHOLDER_RECENTLY_SHIPPED,
    placeholder: true,
  };
}
