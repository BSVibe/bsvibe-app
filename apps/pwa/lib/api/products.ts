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
import type { FileTreeEntry, Product, ProductCreate, ProductFileContent } from "./types";

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

/** List one directory level of a product's repo `main` tree (lazy — call again
 *  with a subdir `path` to expand a folder). Root when `path` is omitted. */
export function listProductFiles(productId: string, path = ""): Promise<FileTreeEntry[]> {
  const qs = path ? `?path=${encodeURIComponent(path)}` : "";
  return apiFetch<FileTreeEntry[]>(`/api/v1/products/${productId}/files${qs}`);
}

/** Read one file's content from a product's repo `main` checkout. */
export function getProductFileContent(
  productId: string,
  path: string,
): Promise<ProductFileContent> {
  return apiFetch<ProductFileContent>(
    `/api/v1/products/${productId}/files/content?path=${encodeURIComponent(path)}`,
  );
}
