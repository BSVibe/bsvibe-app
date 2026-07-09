/**
 * Knowledge-graph TYPE legend labels. The backend stamps each concept node with
 * an ontology `kind` (Pattern / Principle / TechInsight / DomainModel, or the
 * generic `concept`). Those are English enum identifiers; the legend must render
 * them in the workspace language while the `kind` value itself (colour / filter
 * key) stays the English identifier.
 */

import { GRAPH_KINDS, graphKindLabel, humanizeGroup } from "@/lib/i18n/graph-kinds";
import enMessages from "@/messages/en.json";
import koMessages from "@/messages/ko.json";
import { describe, expect, it } from "vitest";

// A fake translator that echoes the key, so we can assert WHICH catalog key a
// group resolves to without depending on catalog copy.
const echo = (key: string) => key;

describe("graphKindLabel", () => {
  it("maps every known ontology kind to its graphKind.* catalog key", () => {
    for (const kind of GRAPH_KINDS) {
      expect(graphKindLabel(kind, echo)).toBe(`graphKind.${kind}`);
    }
  });

  it("humanizes an unknown group id instead of a catalog lookup", () => {
    expect(graphKindLabel("some-new-kind", echo)).toBe("Some new kind");
  });

  it("falls back to the Other label for an empty group", () => {
    expect(graphKindLabel("", echo)).toBe("graphGroupOther");
  });
});

describe("humanizeGroup", () => {
  it("title-cases and de-slugs, dropping a leading underscore", () => {
    expect(humanizeGroup("api-design")).toBe("Api design");
    expect(humanizeGroup("_cluster")).toBe("Cluster");
    expect(humanizeGroup("")).toBeNull();
  });
});

describe("catalogs carry a label for every ontology kind", () => {
  it("en + ko both localize each graphKind, and ko is actually Korean", () => {
    const catalog = (m: unknown) =>
      (m as { knowledge: { graphKind: Record<string, string> } }).knowledge.graphKind;
    const en = catalog(enMessages);
    const ko = catalog(koMessages);
    for (const kind of GRAPH_KINDS) {
      expect(en[kind]).toBeTruthy();
      expect(ko[kind]).toBeTruthy();
    }
    // At least one kind renders as Hangul in the ko catalog (Pattern → 패턴).
    expect(/[가-힣]/.test(ko.Pattern)).toBe(true);
  });
});
