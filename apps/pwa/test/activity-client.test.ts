/**
 * Activity view-model client — wire contracts against a mocked fetch.
 *
 *  - getActivity(): composes /api/v1/runs + /api/v1/products into calm
 *    ActivityRun rows (product slug resolved by run.product_id, status mapped to
 *    plain language + tone). Mirrors the backend Run/Product shapes 1:1.
 *  - getRunDeliverables(): GETs /api/v1/deliverables?run_id=<id> and maps the
 *    rows to the calm ActivityDeliverable vocabulary.
 */

import { getActivity, getRunDeliverables } from "@/lib/api/activity";
import { ApiError } from "@/lib/api/client";
import type { Deliverable, Product, Run } from "@/lib/api/types";
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

const PRODUCT: Product = {
  id: "prod-1",
  workspace_id: "ws-1",
  name: "Blog",
  slug: "blog",
  repo_url: null,
  created_at: NOW,
  updated_at: NOW,
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Route-aware fetch: runs / products / deliverables each return a body (or a
 *  Response to force a failure). */
function installFetch(opts: {
  runs?: () => Run[] | Response;
  products?: () => Product[] | Response;
  deliverables?: () => Deliverable[] | Response;
}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/v1/runs")) {
      const r = opts.runs?.() ?? [];
      return r instanceof Response ? r : json(r);
    }
    if (url.startsWith("/api/v1/products")) {
      const p = opts.products?.() ?? [];
      return p instanceof Response ? p : json(p);
    }
    if (url.startsWith("/api/v1/deliverables")) {
      const d = opts.deliverables?.() ?? [];
      return d instanceof Response ? d : json(d);
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  global.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

function run(over: Partial<Run> = {}): Run {
  return {
    id: "run-1",
    workspace_id: "ws-1",
    product_id: "prod-1",
    request_id: "req-1",
    status: "shipped",
    created_at: NOW,
    updated_at: NOW,
    ...over,
  };
}

describe("getActivity", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("requests /api/v1/runs with a limit and resolves the product slug", async () => {
    const fetchMock = installFetch({
      runs: () => [run({ product_id: "prod-1", status: "shipped" })],
      products: () => [PRODUCT],
    });

    const rows = await getActivity();

    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      runId: "run-1",
      productSlug: "blog",
      status: "shipped",
      statusLabel: "Shipped",
      tone: "shipped",
    });
    const runUrl = fetchMock.mock.calls
      .map((c) => String(c[0]))
      .find((u) => u.startsWith("/api/v1/runs"));
    expect(runUrl).toContain("limit=50");
  });

  it("degrades to the 'workspace' slug when a run carries no product", async () => {
    installFetch({ runs: () => [run({ product_id: null })], products: () => [PRODUCT] });

    const rows = await getActivity();

    expect(rows[0]?.productSlug).toBe("workspace");
  });

  it("maps each run status to its calm label + tone", async () => {
    installFetch({
      runs: () => [
        run({ id: "a", status: "running" }),
        run({ id: "b", status: "review_ready" }),
        run({ id: "c", status: "failed" }),
        run({ id: "d", status: "open" }),
        run({ id: "e", status: "cancelled" }),
      ],
      products: () => [PRODUCT],
    });

    const rows = await getActivity();
    const byId = Object.fromEntries(rows.map((r) => [r.runId, r]));

    expect(byId.a).toMatchObject({ statusLabel: "Working", tone: "working" });
    expect(byId.b).toMatchObject({ statusLabel: "Needs your review", tone: "review" });
    expect(byId.c).toMatchObject({ statusLabel: "Didn’t finish", tone: "failed" });
    expect(byId.d).toMatchObject({ statusLabel: "Just started", tone: "neutral" });
    expect(byId.e).toMatchObject({ statusLabel: "Stood down", tone: "neutral" });
  });

  it("surfaces an ApiError when the runs read fails", async () => {
    installFetch({ runs: () => json("boom", 500), products: () => [PRODUCT] });

    await expect(getActivity()).rejects.toBeInstanceOf(ApiError);
  });
});

describe("getRunDeliverables", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("GETs /api/v1/deliverables narrowed to the run and maps to the calm shape", async () => {
    const deliverable: Deliverable = {
      id: "d1",
      run_id: "run-1",
      workspace_id: "ws-1",
      deliverable_type: "pr",
      summary: "Add getRelatedPosts\nsecond line",
      artifact_refs: ["src/posts.ts"],
      artifact_uri: "https://github.com/acme/repo/pull/15",
      created_at: NOW,
    };
    const fetchMock = installFetch({ deliverables: () => [deliverable] });

    const items = await getRunDeliverables("run-1");

    expect(items).toHaveLength(1);
    expect(items[0]).toEqual({
      id: "d1",
      title: "Add getRelatedPosts",
      artifactType: "pr",
      source: "opened a pull request",
      verdict: "This is verified",
      link: "https://github.com/acme/repo/pull/15",
    });
    const url = String(fetchMock.mock.calls[0]?.[0]);
    expect(url).toContain("run_id=run-1");
    expect(url).toContain("limit=50");
  });

  it("omits the link and uses a calm title fallback when none is present", async () => {
    const deliverable: Deliverable = {
      id: "d2",
      run_id: "run-1",
      workspace_id: "ws-1",
      deliverable_type: "code",
      summary: null,
      artifact_refs: [],
      artifact_uri: null,
      created_at: NOW,
    };
    installFetch({ deliverables: () => [deliverable] });

    const items = await getRunDeliverables("run-1");

    expect(items[0]).toEqual({
      id: "d2",
      title: "Delivered artifact",
      artifactType: "file",
      source: "committed to the repo",
      verdict: "This is verified",
    });
    expect(items[0]).not.toHaveProperty("link");
  });

  it("surfaces an ApiError when the deliverables read fails", async () => {
    installFetch({ deliverables: () => json("nope", 403) });

    await expect(getRunDeliverables("run-1")).rejects.toBeInstanceOf(ApiError);
  });
});
