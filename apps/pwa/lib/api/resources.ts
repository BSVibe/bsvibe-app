/** Product resources API — REAL backend `/api/v1/products/{id}/resources`
 *  (active workspace scope; the product must be in the caller's workspace or
 *  the backend 404s).
 *
 *   GET    /api/v1/products/{id}/resources           — list a product's resources
 *   POST   /api/v1/products/{id}/resources           — add one; 201 ResourceResponse.
 *                                                      Body mirrors the backend
 *                                                      ResourceCreate (extra=forbid)
 *                                                      1:1 — kind + title, with
 *                                                      url/note omitted entirely
 *                                                      when blank.
 *   DELETE /api/v1/products/{id}/resources/{rid}      — remove one; 204. */

import { apiFetch } from "./client";
import type { ProductResource, ProductResourceCreate } from "./types";

/** Resources for a product in the caller's resolved active workspace. */
export function listResources(productId: string): Promise<ProductResource[]> {
  return apiFetch<ProductResource[]>(`/api/v1/products/${productId}/resources`);
}

/** Add a resource to a product. Builds the body to match the backend
 *  extra=forbid schema: always send `kind` + `title`; include `url` / `note`
 *  only when non-blank (never sent as empty strings). */
export function addResource(
  productId: string,
  input: ProductResourceCreate,
): Promise<ProductResource> {
  const body: ProductResourceCreate = { kind: input.kind, title: input.title.trim() };
  const url = input.url?.trim();
  if (url) body.url = url;
  const note = input.note?.trim();
  if (note) body.note = note;

  return apiFetch<ProductResource>(`/api/v1/products/${productId}/resources`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Remove a resource from a product. */
export function removeResource(productId: string, resourceId: string): Promise<void> {
  return apiFetch<void>(`/api/v1/products/${productId}/resources/${resourceId}`, {
    method: "DELETE",
  });
}
