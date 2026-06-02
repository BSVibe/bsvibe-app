/** Trust proof-surface wire types (Lift M4b — PWA mirror of M4a backend).
 *
 *  Mirrors the Pydantic schemas in `backend/api/v1/inside/trust.py`. Kept in
 *  a sibling `.types.ts` file so the client (`trust.ts`) stays minimal and
 *  the wire shapes are easy to spot in code-review.
 *
 *  Goodhart note: the wire shape carries raw counts for the L3 Inside panel,
 *  but the L0 Fleet glance renders ONLY the glyph (design §3.2). Components
 *  must not surface `touch_time` / `deposit_rate` numbers on a Fleet card.
 */

/** L0 trend-arrow glyph vocabulary (design §3.2 four-state set). */
export type TrendGlyph = "↗" | "→" | "↘" | "·";

/** L0 trend-arrow + plain-language reason (backs the hover tooltip). */
export interface TrendArrow {
  glyph: TrendGlyph;
  reason: string;
}

/** L3 founder touch time over the rolling window. */
export interface TouchTime {
  total_touch_time_hours: number;
  decisions_resolved_count: number;
  decisions_pending_count: number;
  window_days: number;
}

/** L3 deposit rate — verified-run garden notes per window. */
export interface DepositRate {
  deposit_count: number;
  slope_per_day: number;
  window_days: number;
}

/** L3 goodhart cross-check (design §2.1 Signal A + B). */
export interface ContractStrength {
  is_steady: boolean;
  amber_reason: string | null;
}

/** One product lane on the Fleet glance. */
export interface FleetTrustEntry {
  product_id: string;
  trend_arrow: TrendArrow;
}

/** Workspace-wide product trust glyphs (L0). */
export interface FleetTrustResponse {
  products: FleetTrustEntry[];
}

/** Single-product trust detail (L3 Inside trust strip). */
export interface ProductTrustResponse {
  product_id: string;
  touch_time: TouchTime;
  deposit_rate: DepositRate;
  trend_arrow: TrendArrow;
  contract_strength: ContractStrength;
}
