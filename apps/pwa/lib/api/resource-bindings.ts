/** Resource bindings API — REAL backend `/api/v1/products/{id}/bindings`
 *  (Workflow §3): the per-Product × ConnectorAccount 3-knob binding carrying
 *  `selection`, `trigger {enabled, filters}`, and `output_mode {safe|direct}`.
 *
 *   GET    /api/v1/products/{id}/bindings              — list a product's bindings
 *   POST   /api/v1/products/{id}/bindings              — create one; 201
 *   PATCH  /api/v1/products/{id}/bindings/{bid}        — partial knob update
 *   DELETE /api/v1/products/{id}/bindings/{bid}        — remove one; 204
 *
 *  Bodies mirror the backend extra=forbid schemas 1:1: only declared fields
 *  are sent, and defaults are server-side (we don't pre-fill them on the wire).
 */

import { apiFetch } from "./client";
import type { ResourceBinding, ResourceBindingCreate, ResourceBindingUpdate } from "./types";

/** Bindings for a product in the caller's resolved active workspace. */
export function listBindings(productId: string): Promise<ResourceBinding[]> {
  return apiFetch<ResourceBinding[]>(`/api/v1/products/${productId}/bindings`);
}

/** Create a per-Product × Connector binding. The body matches
 *  `ResourceBindingCreate` exactly — only `connector_account_id` + `resource_id`
 *  are required; defaults for `selection` / `trigger` / `output_mode` live
 *  server-side, so we omit them when the caller didn't set them. */
export function createBinding(
  productId: string,
  input: ResourceBindingCreate,
): Promise<ResourceBinding> {
  const body: ResourceBindingCreate = {
    connector_account_id: input.connector_account_id,
    resource_id: input.resource_id.trim(),
  };
  if (input.selection !== undefined) body.selection = input.selection;
  if (input.trigger !== undefined) body.trigger = input.trigger;
  if (input.output_mode !== undefined) body.output_mode = input.output_mode;

  return apiFetch<ResourceBinding>(`/api/v1/products/${productId}/bindings`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** PATCH a knob (or two) on an existing binding. Every field is optional —
 *  pass only what's changing. */
export function updateBinding(
  productId: string,
  bindingId: string,
  patch: ResourceBindingUpdate,
): Promise<ResourceBinding> {
  return apiFetch<ResourceBinding>(`/api/v1/products/${productId}/bindings/${bindingId}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

/** Remove a binding. 204 No Content → resolves to void. */
export function removeBinding(productId: string, bindingId: string): Promise<void> {
  return apiFetch<void>(`/api/v1/products/${productId}/bindings/${bindingId}`, {
    method: "DELETE",
  });
}
