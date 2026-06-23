/**
 * buildReviewLookup (lib/api/review-context.ts) — the join that gives every
 * "needs your judgment" surface a concise title + a link to the proof, so the
 * founder's final approval is never blind.
 */

import { buildReviewLookup } from "@/lib/api/review-context";
import type { Deliverable, Product, Run } from "@/lib/api/types";
import { describe, expect, it } from "vitest";

const NOW = "2026-06-24T00:00:00Z";

function product(id: string, slug: string): Product {
  return {
    id,
    workspace_id: "ws-1",
    name: slug,
    slug,
    repo_url: null,
    created_at: NOW,
    updated_at: NOW,
  } as Product;
}

function run(id: string, productId: string | null, intent: string | null): Run {
  return {
    id,
    workspace_id: "ws-1",
    product_id: productId,
    request_id: null,
    status: "review_ready",
    intent,
    created_at: NOW,
    updated_at: NOW,
  } as Run;
}

function deliverable(id: string, runId: string, summary: string | null): Deliverable {
  return {
    id,
    workspace_id: "ws-1",
    run_id: runId,
    deliverable_type: "pr",
    summary,
    artifact_refs: [],
    artifact_uri: null,
    verified: true,
    created_at: NOW,
  };
}

const PRODUCTS = [product("p-1", "bsvibe-app")];
const RUNS = [run("r-1", "p-1", "Add factorial utility"), run("r-2", "p-1", "Refactor adapters")];
const DELIVERABLES = [deliverable("d-1", "r-1", "Add factorial(n) with ValueError on negatives.")];

describe("buildReviewLookup", () => {
  it("forDelivery prefers the deliverable summary as title and links to the proof", () => {
    const lookup = buildReviewLookup(RUNS, DELIVERABLES, PRODUCTS);
    const ctx = lookup.forDelivery("d-1", "r-1");
    expect(ctx.title).toBe("Add factorial(n) with ValueError on negatives.");
    expect(ctx.productSlug).toBe("bsvibe-app");
    expect(ctx.deliverableId).toBe("d-1");
    expect(ctx.detailHref).toBe("/deliverables/d-1");
  });

  it("forDelivery recovers the run id from the deliverable when not supplied", () => {
    const lookup = buildReviewLookup(RUNS, DELIVERABLES, PRODUCTS);
    const ctx = lookup.forDelivery("d-1", null);
    expect(ctx.runId).toBe("r-1");
    expect(ctx.productSlug).toBe("bsvibe-app");
  });

  it("forRun falls back to the run intent + a /runs link when no deliverable", () => {
    const lookup = buildReviewLookup(RUNS, DELIVERABLES, PRODUCTS);
    const ctx = lookup.forRun("r-2");
    expect(ctx.title).toBe("Refactor adapters");
    expect(ctx.deliverableId).toBeNull();
    expect(ctx.detailHref).toBe("/runs/r-2");
  });

  it("forRun uses the deliverable proof link when the run has one", () => {
    const lookup = buildReviewLookup(RUNS, DELIVERABLES, PRODUCTS);
    const ctx = lookup.forRun("r-1");
    expect(ctx.deliverableId).toBe("d-1");
    expect(ctx.detailHref).toBe("/deliverables/d-1");
  });

  it("degrades calmly for an unknown id (no title, no link, workspace slug)", () => {
    const lookup = buildReviewLookup(RUNS, DELIVERABLES, PRODUCTS);
    const ctx = lookup.forDelivery("nope", null);
    expect(ctx.title).toBeNull();
    expect(ctx.detailHref).toBeNull();
    expect(ctx.productSlug).toBe("workspace");
  });
});
