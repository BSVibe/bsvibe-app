/** Products API — REAL backend `/api/v1/products` (active workspace scope).
 *
 *   GET  /api/v1/products   — list products in the active workspace
 *   POST /api/v1/products   — create one; 201 `ProductResponse`. The body
 *                             mirrors the backend `ProductCreate` (extra=forbid)
 *                             1:1 — name + slug, with `repo_url` omitted entirely
 *                             when blank (never sent as an empty string). A
 *                             duplicate slug 409s; the caller surfaces that
 *                             calmly. */

import { apiFetch } from "./client";
import type { Product, ProductCreate } from "./types";

/** Products in the caller's resolved active workspace. */
export function listProducts(): Promise<Product[]> {
  return apiFetch<Product[]>("/api/v1/products");
}

/** Create a product. Builds the body to match the backend extra=forbid schema:
 *  always send `name` + `slug`; include `repo_url` only when non-blank. */
export function createProduct(input: ProductCreate): Promise<Product> {
  const body: ProductCreate = { name: input.name, slug: input.slug };
  const repoUrl = input.repo_url?.trim();
  if (repoUrl) body.repo_url = repoUrl;

  return apiFetch<Product>("/api/v1/products", {
    method: "POST",
    body: JSON.stringify(body),
  });
}
