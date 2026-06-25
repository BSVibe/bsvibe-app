import type { useTranslations } from "next-intl";

/** L11 — humanize the RAW internal run failure reason.
 *
 *  A failed/cancelled run records an engineer-facing `reason` on the backend
 *  (`ExecutionRunHistory.reason`), e.g.
 *    "loop crashed: executor chat task 61483a05-… failed: exit 1"
 *    "agent loop system error"
 *  The founder shouldn't have to read a UUID-laden stack-y string. This pure
 *  helper maps the known raw shapes (case-insensitive, most-specific first) onto
 *  a calm, plain-language sentence in the workspace language (via the passed-in
 *  next-intl `t`, scoped to the `run` namespace). An unknown reason degrades to
 *  a generic line — it NEVER surfaces the raw, UUID-laden string as the primary
 *  text.
 *
 *  Retroactive / frontend-only: no backend change. The raw reason can still be
 *  kept available behind a collapsed "Technical details" disclosure.
 */
type RunT = ReturnType<typeof useTranslations<"run">>;

export function humanizeFailureReason(raw: string, t: RunT): string {
  const lower = (raw ?? "").toLowerCase();

  // Most-specific first: an executor-chat crash signature (with an exit code)
  // is a distinct, common shape and must win over the bare "loop crashed".
  if (/executor chat task/.test(lower) && /\bexit\b/.test(lower)) {
    return t("failureHuman.executorCrash");
  }

  // You stopped it — read it naturally, not as a "failure".
  if (/founder\s+cancel/.test(lower) || /founder\s+discard/.test(lower)) {
    return t("failureHuman.cancelled");
  }

  if (lower.includes("timed out") || lower.includes("timeout")) {
    return t("failureHuman.timeout");
  }

  if (lower.includes("sandbox")) {
    return t("failureHuman.sandbox");
  }

  if (lower.includes("system error")) {
    return t("failureHuman.systemError");
  }

  // Generic crash — catch after the specific shapes above.
  if (lower.includes("loop crashed")) {
    return t("failureHuman.loopCrashed");
  }

  // Unknown: a calm generic line — never the raw UUID-laden string.
  return t("failureHuman.generic");
}

/** Strip UUIDs and long hex hashes from a free-form string, for any place that
 *  DOES surface the raw text (e.g. a collapsed "Technical details" disclosure)
 *  so an engineer-facing id never leaks into founder-visible copy. */
export function stripIds(raw: string): string {
  return (
    (raw ?? "")
      // UUIDs (8-4-4-4-12).
      .replace(/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi, "")
      // Long bare hex hashes (sha-ish, 12+ chars).
      .replace(/\b[0-9a-f]{12,}\b/gi, "")
      // Collapse the whitespace the removals left behind.
      .replace(/\s{2,}/g, " ")
      .trim()
  );
}
