/** Products API — REAL backend `/api/v1/products` (active workspace scope). */

import { apiFetch } from "./client";
import type { Product } from "./types";

/** Products in the caller's resolved active workspace. */
export function listProducts(): Promise<Product[]> {
  return apiFetch<Product[]>("/api/v1/products");
}
