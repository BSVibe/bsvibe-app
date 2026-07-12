/** Run-routing API — REAL backend `/api/v1/run-routing`
 *  (backend/api/v1/run_routing.py): the founder's single ROUTING surface. Each
 *  rule maps a natural-language CONDITION (`source_text`) to a target
 *  ModelAccount; the backend compiles the phrase into structured caller_id /
 *  conditions on save (transparent to the founder). The dispatch resolver
 *  consumes these rows at runtime (we only read/write the rows here).
 *
 *   GET    /api/v1/run-routing        — list rules for the workspace
 *   POST   /api/v1/run-routing        — create one from {name, source_text, target}
 *   PATCH  /api/v1/run-routing/{id}   — edit {source_text?, target?}; editing
 *                                       source_text recompiles server-side
 *   DELETE /api/v1/run-routing/{id}   — delete a rule, 204 No Content
 *
 *  A 422 on create/update means the phrase compiled to nothing valid — the
 *  `ApiError.detail` carries a rephrase hint the surface renders inline. */

import { apiFetch } from "./client";
import type { RunRoutingRule, RunRoutingRuleCreate, RunRoutingRuleUpdate } from "./types";

/** Run-routing rules for the active workspace, priority ascending. */
export function listRunRoutingRules(): Promise<RunRoutingRule[]> {
  return apiFetch<RunRoutingRule[]>("/api/v1/run-routing");
}

/** Create a run-routing rule. The NL surface sends only `name` / `source_text`
 *  / `target`; the backend compiles `source_text` into the structured
 *  caller_id / conditions. A 422 surfaces via `ApiError.detail`. */
export function createRunRoutingRule(input: RunRoutingRuleCreate): Promise<RunRoutingRule> {
  const body: RunRoutingRuleCreate = { name: input.name, target: input.target };
  if (input.source_text) body.source_text = input.source_text;
  // Structured fields kept for back-compat callers; the NL surface omits them.
  if (input.caller_id) body.caller_id = input.caller_id;
  if (input.priority !== undefined) body.priority = input.priority;
  if (input.is_default !== undefined) body.is_default = input.is_default;
  if (input.is_active !== undefined) body.is_active = input.is_active;
  if (input.conditions && input.conditions.length > 0) body.conditions = input.conditions;

  return apiFetch<RunRoutingRule>("/api/v1/run-routing", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Edit an existing run-routing rule (PATCH). Editing `source_text` recompiles
 *  the caller_id/conditions server-side; a 422 surfaces via `ApiError.detail`. */
export function updateRunRoutingRule(
  id: string,
  patch: RunRoutingRuleUpdate,
): Promise<RunRoutingRule> {
  return apiFetch<RunRoutingRule>(`/api/v1/run-routing/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

/** Delete (remove) a run-routing rule. 204 No Content, so this resolves void. */
export function deleteRunRoutingRule(id: string): Promise<void> {
  return apiFetch<void>(`/api/v1/run-routing/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}
