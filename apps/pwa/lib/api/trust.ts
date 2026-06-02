/** Trust proof-surface API client (Lift M4b).
 *
 *   GET /api/v1/inside/trust/fleet         — every product's trend-arrow glyph
 *   GET /api/v1/inside/trust/{product_id}  — single-product trust detail
 *
 *  Both endpoints are workspace-scoped server-side (RLS + middleware). On the
 *  client they are plain reads — no caching layer, no SSE. Per design §3.4
 *  the Fleet is a glance, not a monitor; pages re-read on navigation.
 */

import { apiFetch } from "./client";
import type { FleetTrustResponse, ProductTrustResponse } from "./trust.types";

/** Workspace-wide product trend-arrow glyphs (L0 Fleet glance). */
export function getFleetTrust(): Promise<FleetTrustResponse> {
  return apiFetch<FleetTrustResponse>("/api/v1/inside/trust/fleet");
}

/** Per-product trust detail (L3 Inside trust strip). */
export function getProductTrust(productId: string): Promise<ProductTrustResponse> {
  return apiFetch<ProductTrustResponse>(`/api/v1/inside/trust/${productId}`);
}
