/** Run-routing API — REAL backend `/api/v1/run-routing`
 *  (backend/api/v1/run_routing.py): the founder's single ROUTING surface after
 *  the Layer-2 model-routing layer was hard-deleted. A rule maps a dispatch
 *  caller's work to a target ModelAccount; the dispatch resolver consumes these
 *  rows at runtime (we only read/write the rows here).
 *
 *   GET    /api/v1/run-routing           — list rules for the workspace,
 *                                          priority ascending
 *   GET    /api/v1/run-routing/callers   — the selectable dispatch callers
 *                                          (single source of truth for the form)
 *   POST   /api/v1/run-routing           — create one (201 RunRuleResponse)
 *   DELETE /api/v1/run-routing/{id}      — delete a rule, 204 No Content
 *
 *  The create body mirrors the backend `RunRuleCreate` (extra=forbid) 1:1: we
 *  send name / target / priority / is_default, include `caller_id` only when set
 *  (the catch-all default omits it), and DROP `conditions` entirely when none
 *  are given so the wire shape stays minimal. */

import { apiFetch } from "./client";
import type {
  RunRoutingCaller,
  RunRoutingCompileResult,
  RunRoutingRule,
  RunRoutingRuleCreate,
  RunRoutingRuleUpdate,
} from "./types";

/** Run-routing rules for the active workspace, priority ascending. */
export function listRunRoutingRules(): Promise<RunRoutingRule[]> {
  return apiFetch<RunRoutingRule[]>("/api/v1/run-routing");
}

/** The dispatch callers a non-default rule may target (registry-backed). */
export function listRunRoutingCallers(): Promise<RunRoutingCaller[]> {
  return apiFetch<RunRoutingCaller[]>("/api/v1/run-routing/callers");
}

/** Create a run-routing rule. Builds the body to match the backend
 *  extra=forbid schema: always send name / target / priority / is_default;
 *  include `caller_id` only when set, and `conditions` only when non-empty. */
export function createRunRoutingRule(input: RunRoutingRuleCreate): Promise<RunRoutingRule> {
  const body: RunRoutingRuleCreate = {
    name: input.name,
    target: input.target,
    priority: input.priority,
    is_default: input.is_default ?? false,
  };
  if (input.caller_id) body.caller_id = input.caller_id;
  if (input.is_active !== undefined) body.is_active = input.is_active;
  if (input.conditions && input.conditions.length > 0) body.conditions = input.conditions;

  return apiFetch<RunRoutingRule>("/api/v1/run-routing", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Compile a plain-language routing description into rule PROPOSALS (dry-run —
 *  nothing is persisted; the caller previews then applies via createRunRoutingRule). */
export function compileRunRoutingRules(text: string): Promise<RunRoutingCompileResult> {
  return apiFetch<RunRoutingCompileResult>("/api/v1/run-routing/compile", {
    method: "POST",
    body: JSON.stringify({ text }),
  });
}

/** Edit an existing run-routing rule (PATCH — caller / target / active). */
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
