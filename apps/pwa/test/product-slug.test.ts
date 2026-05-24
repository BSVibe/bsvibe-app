/**
 * Product slug helpers — pure functions that power the create form's
 * auto-suggest + pre-submit validation. The backend slug grammar is
 * `^[a-z][a-z0-9-]*$` (backend/api/v1/products.py `_SLUG_RE`).
 */

import { isValidSlug, suggestSlug } from "@/lib/products/slug";
import { describe, expect, it } from "vitest";

describe("suggestSlug", () => {
  it("lowercases and turns spaces into hyphens", () => {
    expect(suggestSlug("Related Posts")).toBe("related-posts");
  });

  it("strips characters outside [a-z0-9-]", () => {
    expect(suggestSlug("Acme's Widgets!")).toBe("acmes-widgets");
  });

  it("collapses runs of separators into a single hyphen", () => {
    expect(suggestSlug("a   b__c")).toBe("a-b-c");
  });

  it("drops leading digits/hyphens so the first char is a letter", () => {
    expect(suggestSlug("123 go")).toBe("go");
    expect(suggestSlug("-- dash")).toBe("dash");
  });

  it("trims trailing hyphens", () => {
    expect(suggestSlug("trailing - ")).toBe("trailing");
  });

  it("returns an empty string when nothing survives", () => {
    expect(suggestSlug("!!!")).toBe("");
    expect(suggestSlug("123")).toBe("");
  });
});

describe("isValidSlug", () => {
  it("accepts a slug matching ^[a-z][a-z0-9-]*$", () => {
    expect(isValidSlug("related-posts")).toBe(true);
    expect(isValidSlug("a")).toBe(true);
    expect(isValidSlug("a1-b2")).toBe(true);
  });

  it("rejects an empty string", () => {
    expect(isValidSlug("")).toBe(false);
  });

  it("rejects a slug that doesn't start with a letter", () => {
    expect(isValidSlug("1abc")).toBe(false);
    expect(isValidSlug("-abc")).toBe(false);
  });

  it("rejects uppercase and invalid characters", () => {
    expect(isValidSlug("Abc")).toBe(false);
    expect(isValidSlug("a b")).toBe(false);
    expect(isValidSlug("a_b")).toBe(false);
  });
});
