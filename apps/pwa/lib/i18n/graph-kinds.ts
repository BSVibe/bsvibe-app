/**
 * Knowledge-graph TYPE legend labels.
 *
 * The backend stamps each concept node with an ontology `kind` — one of the E20
 * seedling types (Pattern / Principle / TechInsight / DomainModel) or the
 * generic `concept` fallback. Those are English enum IDENTIFIERS: the graph uses
 * them as the colour / filter key and they must stay stable. Only the LABEL the
 * founder reads is localized (founder decision 2026-07 — same rule as the concept
 * node display labels: identity English, display follows the workspace language).
 */

/** The ontology kinds the backend emits as concept-node `kind` values. */
export const GRAPH_KINDS = [
  "Pattern",
  "Principle",
  "TechInsight",
  "DomainModel",
  "concept",
] as const;

/** Humanize an unknown group id ("api-design" → "Api design", "_cluster" →
 *  "Cluster"); `null` for an empty id. Used for community ids and any kind the
 *  ontology list above doesn't cover. */
export function humanizeGroup(group: string): string | null {
  if (!group) return null;
  const cleaned = group.replace(/^_/, "").replace(/[-_]/g, " ");
  return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
}

/**
 * The localized label for a TYPE-mode legend group. A known ontology kind
 * resolves from the `knowledge.graphKind.*` catalog (so it follows the workspace
 * language); an unknown id is humanized from the identifier; an empty id gets the
 * generic "Other" label. The `kind` value itself is never changed — only its
 * display.
 */
export function graphKindLabel(group: string, t: (key: string) => string): string {
  if (!group) return t("graphGroupOther");
  if ((GRAPH_KINDS as readonly string[]).includes(group)) return t(`graphKind.${group}`);
  return humanizeGroup(group) ?? t("graphGroupOther");
}
