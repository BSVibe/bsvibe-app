/** Human, localizable labels for dispatch caller_ids.
 *
 * The routing form/list should never show a raw technical id like
 * `workflow.agent_loop.plan` to the founder. Each known caller maps to a short
 * label key under `settings.models.routing.callerLabels` (translated per locale);
 * skill callers show their bare name; anything unknown falls back to the raw id.
 */

const KEY_BY_CALLER: Record<string, string> = {
  "workflow.agent_loop.plan": "plan",
  "workflow.agent_loop.act": "act",
  "workflow.frame": "frame",
  "workflow.judge": "judge",
  "workflow.settle.extract": "settle",
  "knowledge.ingest": "ingest",
  "knowledge.query": "query",
  "knowledge.canonicalization": "canonicalization",
  "chat.completions": "chat",
  "routing.compile": "compile",
};

const SKILL_PREFIX = "skill.";

/** The `callerLabels.<key>` translation key for a known caller, else null. */
export function callerLabelKey(callerId: string): string | null {
  return KEY_BY_CALLER[callerId] ?? null;
}

/** A skill caller's bare name (strips `skill.`), else null. */
export function skillCallerName(callerId: string): string | null {
  return callerId.startsWith(SKILL_PREFIX) ? callerId.slice(SKILL_PREFIX.length) : null;
}

/** Resolve a caller_id to a display label: a localized label for known callers
 *  (via `translate`), the bare name for `skill.<name>`, else the raw id. */
export function callerDisplay(
  callerId: string | null | undefined,
  translate: (key: string) => string,
): string {
  if (!callerId) return "";
  const key = callerLabelKey(callerId);
  if (key) return translate(`callerLabels.${key}`);
  return skillCallerName(callerId) ?? callerId;
}
