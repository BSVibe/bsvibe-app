/**
 * brief.ts real-data composition — drives getBrief() against a mocked fetch and
 * asserts it maps /api/v1/{products,runs,decisions,safemode/queue} into the
 * BriefView shape (run.status → lane state, needs-you count, recently shipped).
 */

import { getBrief } from "@/lib/api/brief";
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

function product(id: string, slug: string, name: string) {
  return {
    id,
    workspace_id: "ws-1",
    name,
    slug,
    repo_url: null,
    created_at: NOW,
    updated_at: NOW,
  };
}

function run(id: string, product_id: string | null, status: string) {
  return {
    id,
    workspace_id: "ws-1",
    product_id,
    request_id: null,
    status,
    created_at: NOW,
    updated_at: NOW,
  };
}

function deliverable(
  id: string,
  deliverable_type: string,
  summary: string | null,
  artifact_uri: string | null = null,
  artifact_refs: string[] = [],
) {
  return {
    id,
    run_id: `run-${id}`,
    workspace_id: "ws-1",
    deliverable_type,
    summary,
    artifact_refs,
    artifact_uri,
    created_at: NOW,
  };
}

/** Route a mocked fetch by path → JSON body. */
function mockFetch(routes: Record<string, unknown>) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    for (const [path, body] of Object.entries(routes)) {
      if (url.startsWith(path)) {
        return new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
    }
    return new Response("not found", { status: 404 });
  });
}

describe("getBrief (real-data composition)", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("derives lanes from products + each product's latest run status", async () => {
    global.fetch = mockFetch({
      "/api/v1/products": [
        product("p-running", "alpha", "alpha"),
        product("p-open", "beta", "beta"),
        product("p-review", "gamma", "gamma"),
        product("p-shipped", "delta", "delta"),
        product("p-none", "epsilon", "epsilon"),
      ],
      "/api/v1/runs": [
        run("r1", "p-running", "running"),
        run("r2", "p-open", "open"),
        run("r3", "p-review", "review_ready"),
        run("r4", "p-shipped", "shipped"),
      ],
      "/api/v1/decisions": [],
      "/api/v1/safemode/queue": [],
      "/api/v1/deliverables": [],
    }) as unknown as typeof fetch;

    const view = await getBrief();
    const bySlug = Object.fromEntries(view.lanes.map((l) => [l.slug, l.state]));

    expect(bySlug.alpha).toBe("working");
    expect(bySlug.beta).toBe("triggered");
    expect(bySlug.gamma).toBe("needs-you");
    expect(bySlug.delta).toBe("shipped");
    expect(bySlug.epsilon).toBe("idle"); // no run → idle
    expect(view.lanes).toHaveLength(5);
  });

  it("uses only the newest run per product (list is newest-first)", async () => {
    global.fetch = mockFetch({
      "/api/v1/products": [product("p1", "alpha", "alpha")],
      "/api/v1/runs": [run("newest", "p1", "running"), run("older", "p1", "shipped")],
      "/api/v1/decisions": [],
      "/api/v1/safemode/queue": [],
      "/api/v1/deliverables": [],
    }) as unknown as typeof fetch;

    const view = await getBrief();
    expect(view.lanes[0].state).toBe("working");
  });

  it("counts needs-you from pending proposals + safe-mode queue", async () => {
    global.fetch = mockFetch({
      "/api/v1/products": [product("p1", "alpha", "alpha")],
      "/api/v1/runs": [],
      "/api/v1/decisions": [
        {
          id: "prop-1",
          proposal_kind: "merge",
          action_kind: "merge_notes",
          action_path: "notes/auth",
          status: "pending",
          score: 80,
          created_at: NOW,
          expires_at: null,
        },
      ],
      "/api/v1/safemode/queue": [
        {
          id: "sm-1",
          workspace_id: "ws-1",
          deliverable_id: "d-1",
          status: "pending",
          compensation_tier: null,
          expires_at: NOW,
          extension_count: 0,
          created_at: NOW,
        },
      ],
      "/api/v1/deliverables": [],
    }) as unknown as typeof fetch;

    const view = await getBrief();
    expect(view.needsYou).toHaveLength(2);
    expect(view.needsYou.some((n) => n.question.includes("notes/auth"))).toBe(true);
    expect(view.needsYou.some((n) => n.question.includes("Safe Mode"))).toBe(true);
  });

  it("lists recently shipped from REAL /api/v1/deliverables, newest first", async () => {
    global.fetch = mockFetch({
      "/api/v1/products": [product("p1", "alpha", "alpha")],
      "/api/v1/runs": [],
      "/api/v1/decisions": [],
      "/api/v1/safemode/queue": [],
      "/api/v1/deliverables": [
        deliverable(
          "d-pr",
          "pr",
          "Add getRelatedPosts function\nwith tests",
          "https://github.com/acme/repo/pull/15",
        ),
        deliverable("d-page", "page", "Publish the launch landing page"),
      ],
    }) as unknown as typeof fetch;

    const view = await getBrief();
    expect(view.recentlyShipped).toHaveLength(2);
    expect(view.recentlyShipped.map((s) => s.id)).toEqual(["d-pr", "d-page"]);

    const [pr, page] = view.recentlyShipped;
    // Title = first line of the summary.
    expect(pr.title).toBe("Add getRelatedPosts function");
    // deliverable_type "pr" maps to the UI artifact-type marker "pr".
    expect(pr.artifactType).toBe("pr");
    // artifact_uri is carried as the link when present.
    expect(pr.link).toBe("https://github.com/acme/repo/pull/15");
    expect(pr.verdict).toBe("This is verified");
    // deliverable_type "page" maps to the UI "doc" marker; no uri → no link.
    expect(page.title).toBe("Publish the launch landing page");
    expect(page.artifactType).toBe("doc");
    expect(page.link).toBeUndefined();

    // All three surfaces are now real → no placeholder.
    expect(view.placeholder).toBe(false);
  });

  it("falls back to a calm title when a deliverable has no summary", async () => {
    global.fetch = mockFetch({
      "/api/v1/products": [product("p1", "alpha", "alpha")],
      "/api/v1/runs": [],
      "/api/v1/decisions": [],
      "/api/v1/safemode/queue": [],
      "/api/v1/deliverables": [deliverable("d-code", "code", null)],
    }) as unknown as typeof fetch;

    const view = await getBrief();
    expect(view.recentlyShipped).toHaveLength(1);
    expect(view.recentlyShipped[0].title).toBe("Shipped deliverable");
    expect(view.recentlyShipped[0].artifactType).toBe("file");
  });

  it("condenses a long LLM summary to its first sentence (not the raw blob)", async () => {
    const longSummary =
      "The fibonacci.py file has been successfully created with a function that " +
      "returns the nth Fibonacci number. The implementation:\n" +
      "- handles n=0 and n=1\n- is iterative";
    global.fetch = mockFetch({
      "/api/v1/products": [product("p1", "alpha", "alpha")],
      "/api/v1/runs": [],
      "/api/v1/decisions": [],
      "/api/v1/safemode/queue": [],
      "/api/v1/deliverables": [deliverable("d-fib", "code", longSummary)],
    }) as unknown as typeof fetch;

    const view = await getBrief();
    expect(view.recentlyShipped[0].title).toBe(
      "The fibonacci.py file has been successfully created with a function that returns the nth Fibonacci number.",
    );
  });

  it("hard-caps an over-long single sentence with an ellipsis", async () => {
    const huge = `${"word ".repeat(60).trim()} end`; // ~300 chars, no sentence break
    global.fetch = mockFetch({
      "/api/v1/products": [product("p1", "alpha", "alpha")],
      "/api/v1/runs": [],
      "/api/v1/decisions": [],
      "/api/v1/safemode/queue": [],
      "/api/v1/deliverables": [deliverable("d-long", "code", huge)],
    }) as unknown as typeof fetch;

    const view = await getBrief();
    const title = view.recentlyShipped[0].title;
    expect(title.endsWith("…")).toBe(true);
    expect(title.length).toBeLessThanOrEqual(141);
  });

  it("an empty/fresh workspace yields calm empty states, NOT demo data", async () => {
    global.fetch = mockFetch({
      "/api/v1/products": [],
      "/api/v1/runs": [],
      "/api/v1/decisions": [],
      "/api/v1/safemode/queue": [],
      "/api/v1/deliverables": [],
    }) as unknown as typeof fetch;

    const view = await getBrief();
    expect(view.lanes).toEqual([]);
    expect(view.needsYou).toEqual([]);
    expect(view.recentlyShipped).toEqual([]);
    expect(view.placeholder).toBe(false);
  });

  it("falls back to demo lanes when the core read fails (no error wall)", async () => {
    global.fetch = vi.fn(
      async () => new Response("nope", { status: 500 }),
    ) as unknown as typeof fetch;

    const view = await getBrief();
    expect(view.lanes.length).toBeGreaterThan(0); // demo fallback
    expect(view.placeholder).toBe(true);
  });

  it("does NOT show the placeholder board on a 401 — it propagates so the gate redirects", async () => {
    // window.location is stubbed so apiFetch's 401 handler can no-op its
    // redirect inside the test without touching the real navigation.
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { pathname: "/brief", assign: vi.fn() } as unknown as Location,
    });
    global.fetch = vi.fn(
      async () => new Response("unauthorized", { status: 401 }),
    ) as unknown as typeof fetch;

    // A 401 is auth-expired, not a network blip: it must NOT degrade into the
    // calm demo board — it propagates (the global handler / gate redirects).
    await expect(getBrief()).rejects.toMatchObject({ status: 401 });
  });
});
