/**
 * product-detail.ts real-data composition — drives getProductDetail() against a
 * mocked fetch and asserts it composes the focused per-product view ENTIRELY
 * client-side from the list endpoints:
 *  - finds the product in /api/v1/products by slug (unknown slug → null)
 *  - filters /api/v1/runs by product_id (newest-first preserved)
 *  - derives the header status from the product's LATEST run
 *  - eagerly fetches /api/v1/deliverables?run_id= for the SHIPPED runs only
 *  - degrades a failing per-run deliverables read to no artifacts, not a throw
 *  - a real core-read failure bubbles up (surface renders the inline error)
 */

import { getProductDetail } from "@/lib/api/product-detail";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const NOW = "2026-05-23T00:00:00Z";

function product(id: string, slug: string, name: string, repo_url: string | null = null) {
  return {
    id,
    workspace_id: "ws-1",
    name,
    slug,
    repo_url,
    created_at: NOW,
    updated_at: NOW,
  };
}

function run(id: string, product_id: string | null, status: string, updated_at = NOW) {
  return {
    id,
    workspace_id: "ws-1",
    product_id,
    request_id: null,
    status,
    created_at: NOW,
    updated_at,
  };
}

function deliverable(
  id: string,
  run_id: string,
  deliverable_type: string,
  summary: string | null,
  artifact_uri: string | null = null,
) {
  return {
    id,
    run_id,
    workspace_id: "ws-1",
    deliverable_type,
    summary,
    artifact_refs: [],
    artifact_uri,
    created_at: NOW,
  };
}

/** Route a mocked fetch by path; deliverables routed by `run_id` query. */
function mockFetch(opts: {
  products: unknown[];
  runs: unknown[];
  deliverablesByRun?: Record<string, unknown[]>;
  /** run_ids whose deliverables read should reject (to assert graceful degrade). */
  failDeliverablesFor?: string[];
}) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.startsWith("/api/v1/products")) {
      return new Response(JSON.stringify(opts.products), { status: 200 });
    }
    if (url.startsWith("/api/v1/runs")) {
      return new Response(JSON.stringify(opts.runs), { status: 200 });
    }
    if (url.startsWith("/api/v1/deliverables")) {
      const runId = new URLSearchParams(url.split("?")[1] ?? "").get("run_id") ?? "";
      if (opts.failDeliverablesFor?.includes(runId)) {
        return new Response("boom", { status: 500 });
      }
      const body = opts.deliverablesByRun?.[runId] ?? [];
      return new Response(JSON.stringify(body), { status: 200 });
    }
    return new Response("not found", { status: 404 });
  });
}

describe("getProductDetail (real-data composition)", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("returns null for an unknown slug (calm not-found, not an error)", async () => {
    global.fetch = mockFetch({
      products: [product("p1", "blog", "Blog")],
      runs: [],
    }) as unknown as typeof fetch;

    const view = await getProductDetail("does-not-exist");
    expect(view).toBeNull();
  });

  it("finds the product by slug and surfaces its name + repo", async () => {
    global.fetch = mockFetch({
      products: [
        product("p1", "blog", "Blog", "https://github.com/acme/blog"),
        product("p2", "store", "Store"),
      ],
      runs: [],
    }) as unknown as typeof fetch;

    const view = await getProductDetail("blog");
    expect(view).not.toBeNull();
    expect(view?.id).toBe("p1");
    expect(view?.name).toBe("Blog");
    expect(view?.repoUrl).toBe("https://github.com/acme/blog");
  });

  it("filters runs to this product only, newest-first preserved", async () => {
    global.fetch = mockFetch({
      products: [product("p1", "blog", "Blog"), product("p2", "store", "Store")],
      runs: [
        run("r-new", "p1", "running", "2026-05-23T03:00:00Z"),
        run("r-other", "p2", "shipped"),
        run("r-old", "p1", "shipped", "2026-05-23T01:00:00Z"),
      ],
    }) as unknown as typeof fetch;

    const view = await getProductDetail("blog");
    expect(view?.runs.map((r) => r.runId)).toEqual(["r-new", "r-old"]);
    // No run from the other product leaked in.
    expect(view?.runs.some((r) => r.runId === "r-other")).toBe(false);
  });

  it("derives the header status from the product's LATEST run", async () => {
    global.fetch = mockFetch({
      products: [product("p1", "blog", "Blog")],
      runs: [run("r1", "p1", "review_ready"), run("r0", "p1", "shipped")],
    }) as unknown as typeof fetch;

    const view = await getProductDetail("blog");
    expect(view?.currentTone).toBe("review");
    expect(view?.currentStatus).toBe("Ready for your review.");
  });

  it("gives a calm header when the product has no runs yet", async () => {
    global.fetch = mockFetch({
      products: [product("p1", "blog", "Blog")],
      runs: [],
    }) as unknown as typeof fetch;

    const view = await getProductDetail("blog");
    expect(view?.runs).toEqual([]);
    expect(view?.currentTone).toBe("neutral");
    expect(view?.currentStatus).toMatch(/Nothing running yet/);
    expect(view?.shipped).toEqual([]);
  });

  it("eagerly fetches deliverables for SHIPPED runs only and maps them", async () => {
    const fetchMock = mockFetch({
      products: [product("p1", "blog", "Blog")],
      runs: [run("r-ship", "p1", "shipped"), run("r-work", "p1", "running")],
      deliverablesByRun: {
        "r-ship": [
          deliverable("d1", "r-ship", "pr", "Add related-posts\nwidget", "https://gh/pull/15"),
        ],
      },
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    const view = await getProductDetail("blog");
    expect(view?.shipped).toHaveLength(1);
    const item = view?.shipped[0];
    expect(item?.title).toBe("Add related-posts");
    expect(item?.artifactType).toBe("pr");
    expect(item?.source).toBe("opened a pull request");
    expect(item?.verdict).toBe("This is verified");
    expect(item?.link).toBe("https://gh/pull/15");
    expect(item?.productSlug).toBe("blog");

    // Only the shipped run's deliverables were fetched (running run skipped).
    const delCalls = fetchMock.mock.calls
      .map((c) => String(c[0]))
      .filter((u) => u.startsWith("/api/v1/deliverables"));
    expect(delCalls.some((u) => u.includes("run_id=r-ship"))).toBe(true);
    expect(delCalls.some((u) => u.includes("run_id=r-work"))).toBe(false);
  });

  it("degrades a failing per-run deliverables read to no artifacts (no throw)", async () => {
    global.fetch = mockFetch({
      products: [product("p1", "blog", "Blog")],
      runs: [run("r-ok", "p1", "shipped"), run("r-bad", "p1", "shipped")],
      deliverablesByRun: { "r-ok": [deliverable("d1", "r-ok", "page", "Launch page")] },
      failDeliverablesFor: ["r-bad"],
    }) as unknown as typeof fetch;

    const view = await getProductDetail("blog");
    // The good run's artifact still shows; the failing run just contributes none.
    expect(view?.shipped.map((s) => s.id)).toEqual(["d1"]);
  });

  it("bubbles up a core-read failure (surface shows the inline error)", async () => {
    global.fetch = vi.fn(
      async () => new Response("nope", { status: 500 }),
    ) as unknown as typeof fetch;

    await expect(getProductDetail("blog")).rejects.toBeTruthy();
  });
});
