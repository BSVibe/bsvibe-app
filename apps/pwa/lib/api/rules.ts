/** Routing-rules API — REAL backend `/api/v1/rules`
 *  (backend/api/v1/rules.py): the founder's ROUTING surface. A rule maps a unit
 *  of work to a target model; the gateway's rule engine consumes these rows at
 *  runtime (we never touch that path — only read/write the rows here).
 *
 *   GET    /api/v1/rules         — list routing rules for the active
 *                                  (workspace, account), priority ascending;
 *                                  each carries the conditions it matches on
 *   POST   /api/v1/rules         — create one (201 RuleResponse)
 *   DELETE /api/v1/rules/{id}    — delete a rule, 204 No Content
 *
 *  The create body mirrors the backend `RuleCreate` (extra=forbid) 1:1: we send
 *  only the fields the schema declares and DROP `conditions` entirely when none
 *  are given, so a catch-all / default rule's wire shape stays minimal and the
 *  validator never sees a stray empty array. */

import { apiFetch } from "./client";
import type { RoutingRule, RoutingRuleCreate } from "./types";

/** Routing rules for the active workspace + account, priority ascending. */
export function listRules(): Promise<RoutingRule[]> {
  return apiFetch<RoutingRule[]>("/api/v1/rules");
}

/** Create a routing rule. Builds the body to match the backend extra=forbid
 *  schema: always send name / target_model / priority / is_default, and include
 *  `conditions` only when the caller supplied a non-empty list. */
export function createRule(input: RoutingRuleCreate): Promise<RoutingRule> {
  const body: RoutingRuleCreate = {
    name: input.name,
    target_model: input.target_model,
    priority: input.priority,
    is_default: input.is_default ?? false,
  };
  if (input.is_active !== undefined) body.is_active = input.is_active;
  if (input.conditions && input.conditions.length > 0) body.conditions = input.conditions;

  return apiFetch<RoutingRule>("/api/v1/rules", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Delete (remove) a routing rule. 204 No Content, so this resolves to void. */
export function deleteRule(id: string): Promise<void> {
  return apiFetch<void>(`/api/v1/rules/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}
