/**
 * brief.ts real-data composition — drives getBrief() against a mocked fetch and
 * asserts it folds /api/v1/{products,runs,deliverables} into the merged
 * Work-Home shape: active runs → the "working" hero, done runs → the "stream"
 * (joined to their deliverable).
 *
 * R4 — decisions are UNIFIED back into the Brief: getBrief also folds the
 * pending Safe-Mode deliveries (/api/v1/safemode/queue) + paused-run checkpoints
 * (/api/v1/checkpoints) into a `needsYou` list (the same item shape the existing
 * CheckpointRow / DeliveryRow consume, joined to the run/deliverable for a
 * concise title + proof link). This REVERSES L7 (#6), which had removed the
 * needs-you block from the Brief — decisions now LIVE inline in the Brief, with
 * context, rather than on a divorced inbox tab.
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

function run(id: string, product_id: string | null, status: string, intent: string | null = null) {
  return {
    id,
    workspace_id: "ws-1",
    product_id,
    request_id: null,
    status,
    intent,
    created_at: NOW,
    updated_at: NOW,
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

describe("getBrief (merged Work-Home composition)", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("splits active runs into the hero and done runs into the stream", async () => {
    global.fetch = mockFetch({
      "/api/v1/products": [product("p1", "alpha", "alpha")],
      "/api/v1/runs": [
        run("r-running", "p1", "running", "Write the feature"),
        run("r-open", "p1", "open", "Decompose the direction"),
        run("r-shipped", "p1", "shipped"),
        run("r-failed", "p1", "failed", "Broken link fix"),
      ],
      "/api/v1/decisions": [],
      "/api/v1/safemode/queue": [],
      "/api/v1/deliverables": [],
    }) as unknown as typeof fetch;

    const view = await getBrief();

    // Active (running / open) → the "Working on now" hero, newest-first.
    expect(view.working.map((w) => w.runId)).toEqual(["r-running", "r-open"]);
    expect(view.working[0].title).toBe("Write the feature");
    expect(view.working[0].status).toBe("running");

    // Done (shipped / failed) → the work stream; active runs excluded.
    expect(view.stream.map((s) => s.runId)).toEqual(["r-shipped", "r-failed"]);
    expect(view.placeholder).toBe(false);
  });

  it("L9: a retried run's elapsed clock starts at restarted_at, not created_at", async () => {
    const RESTART = "2026-06-25T12:00:00Z";
    global.fetch = mockFetch({
      "/api/v1/products": [product("p1", "alpha", "alpha")],
      "/api/v1/runs": [
        { ...run("r-retried", "p1", "running", "Retried task"), restarted_at: RESTART },
        run("r-fresh", "p1", "running", "Fresh task"),
      ],
      "/api/v1/decisions": [],
      "/api/v1/safemode/queue": [],
      "/api/v1/deliverables": [],
    }) as unknown as typeof fetch;

    const view = await getBrief();
    const retried = view.working.find((w) => w.runId === "r-retried");
    const fresh = view.working.find((w) => w.runId === "r-fresh");
    expect(retried?.startedAt).toBe(RESTART);
    // A run that was never retried still counts from created_at.
    expect(fresh?.startedAt).toBe(NOW);
  });

  it("R4: DOES carry a needsYou list — decisions are unified back into the Brief (supersedes #6)", async () => {
    global.fetch = mockFetch({
      "/api/v1/products": [product("p1", "alpha", "alpha")],
      "/api/v1/runs": [run("r-paused", "p1", "running", "Build the export")],
      "/api/v1/deliverables": [],
      "/api/v1/safemode/queue": [],
      "/api/v1/checkpoints": [],
      "/api/v1/decisions": [],
    }) as unknown as typeof fetch;

    const view = await getBrief();
    // The merged view now models decisions inline (R4 unification).
    expect("needsYou" in view).toBe(true);
    expect(Array.isArray(view.needsYou)).toBe(true);
  });

  it("R4: folds a paused-run checkpoint + a held delivery into needsYou with joined context", async () => {
    global.fetch = mockFetch({
      "/api/v1/products": [product("p1", "alpha", "alpha")],
      "/api/v1/runs": [
        {
          ...run("r-paused", "p1", "running", "raw direction"),
          summary_title: "Build the export endpoint",
        },
      ],
      "/api/v1/deliverables": [
        deliverable("d-held", "r-paused", "pr", "Add the export endpoint. With docs."),
      ],
      "/api/v1/safemode/queue": [
        { id: "sm-1", deliverable_id: "d-held", run_id: "r-paused", created_at: NOW },
      ],
      "/api/v1/checkpoints": [
        {
          id: "cp-1",
          run_id: "r-paused",
          question: "Ship to prod or staging?",
          options: ["prod", "staging"],
          actions: null,
          decision: "ask_user_question",
          rationale: null,
          prior_decisions: [],
          created_at: NOW,
        },
      ],
      "/api/v1/decisions": [],
    }) as unknown as typeof fetch;

    const view = await getBrief();
    const kinds = view.needsYou.map((n) => n.kind).sort();
    expect(kinds).toEqual(["decision", "delivery"]);
    const checkpoint = view.needsYou.find((n) => n.kind === "decision");
    // Joined context — the checkpoint carries the task title + product, not just
    // the bare question.
    expect(checkpoint?.title).toBe("Build the export endpoint");
    expect(checkpoint?.productSlug).toBe("alpha");
  });

  it("R4: needsYou degrades to empty (never blanks the Brief) when its queues blip", async () => {
    global.fetch = mockFetch({
      "/api/v1/products": [product("p1", "alpha", "alpha")],
      "/api/v1/runs": [],
      "/api/v1/deliverables": [],
      // safemode + checkpoints + decisions intentionally absent → 404 each,
      // each degrades to empty rather than throwing.
    }) as unknown as typeof fetch;

    const view = await getBrief();
    expect(view.needsYou).toEqual([]);
    expect(view.placeholder).toBe(false);
    // ...but "empty" here is NOT the answer "nothing needs you" — every queue
    // failed, so the Brief must say the list could not be read in full.
    expect(view.needsYouIncomplete).toBe(true);
  });

  it("joins a stream row to its deliverable (concise title + report link)", async () => {
    global.fetch = mockFetch({
      "/api/v1/products": [product("p1", "alpha", "alpha")],
      "/api/v1/runs": [run("r-ship", "p1", "shipped", "Add related posts")],
      "/api/v1/decisions": [],
      "/api/v1/safemode/queue": [],
      "/api/v1/deliverables": [
        deliverable("d-pr", "r-ship", "pr", "Add getRelatedPosts function. With tests."),
      ],
    }) as unknown as typeof fetch;

    const view = await getBrief();
    expect(view.stream).toHaveLength(1);
    const row = view.stream[0];
    // Title prefers the deliverable's CONCISE summary (first sentence).
    expect(row.title).toBe("Add getRelatedPosts function.");
    expect(row.deliverableId).toBe("d-pr");
    expect(row.artifactType).toBe("pr");
    expect(row.status).toBe("shipped");
  });

  it("uses the run's intent as the stream title when there's no deliverable", async () => {
    global.fetch = mockFetch({
      "/api/v1/products": [product("p1", "alpha", "alpha")],
      "/api/v1/runs": [run("r-fail", "p1", "failed", "Fix the broken link")],
      "/api/v1/decisions": [],
      "/api/v1/safemode/queue": [],
      "/api/v1/deliverables": [],
    }) as unknown as typeof fetch;

    const view = await getBrief();
    expect(view.stream[0].title).toBe("Fix the broken link");
    expect(view.stream[0].deliverableId).toBeNull();
    expect(view.stream[0].artifactType).toBeNull();
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
    expect(view.working).toEqual([]);
    expect(view.stream).toEqual([]);
    expect(view.placeholder).toBe(false);
  });

  it("degrades to calm empty states when the core read fails (no error wall)", async () => {
    global.fetch = vi.fn(
      async () => new Response("nope", { status: 500 }),
    ) as unknown as typeof fetch;

    const view = await getBrief();
    expect(view.working).toEqual([]);
    expect(view.stream).toEqual([]);
    expect(view.placeholder).toBe(true);
  });

  it("does NOT degrade on a 401 — it propagates so the gate redirects", async () => {
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { pathname: "/brief", assign: vi.fn() } as unknown as Location,
    });
    global.fetch = vi.fn(
      async () => new Response("unauthorized", { status: 401 }),
    ) as unknown as typeof fetch;

    await expect(getBrief()).rejects.toMatchObject({ status: 401 });
  });
  /* A read FAILURE and a measured "no live worker" are different facts, and the
   * Brief acts on this one twice — it revives the onboarding checklist and it
   * flips every active run to "waiting for a worker". Folding a `/workers` blip
   * into `false` makes the UI assert, on a blip, two things it did not measure.
   * Measured 2026-09-03: a 500 on /workers alone put a producing workspace back
   * on the onboarding checklist, telling a founder to "connect a worker" they
   * already had. So the read failure surfaces as `null` — unknown. */
  it("reports the worker signal as UNKNOWN (null) when /workers fails — not a measured false", async () => {
    global.fetch = mockFetch({
      "/api/v1/products": [product("p1", "alpha", "alpha")],
      "/api/v1/runs": [],
      "/api/v1/deliverables": [],
      "/api/v1/safemode/queue": [],
      "/api/v1/checkpoints": [],
      // /api/v1/workers is deliberately unmocked → 404 → the surface's own blip.
    }) as unknown as typeof fetch;

    const view = await getBrief();
    expect(view.hasLiveWorker).toBeNull();
    // ...and the blip must NOT be laundered into the whole-Brief read error.
    expect(view.placeholder).toBe(false);
    expect(view.hasProducts).toBe(true);
  });

  it("control: a /workers read that ANSWERS with no fresh heartbeat is a measured false", async () => {
    global.fetch = mockFetch({
      "/api/v1/products": [product("p1", "alpha", "alpha")],
      "/api/v1/runs": [],
      "/api/v1/deliverables": [],
      "/api/v1/safemode/queue": [],
      "/api/v1/checkpoints": [],
      "/api/v1/workers": [{ id: "w1", name: "mac", status: "online", heartbeat_fresh: false }],
    }) as unknown as typeof fetch;

    const view = await getBrief();
    expect(view.hasLiveWorker).toBe(false);
  });

  it("control: a fresh heartbeat is a measured true", async () => {
    global.fetch = mockFetch({
      "/api/v1/products": [product("p1", "alpha", "alpha")],
      "/api/v1/runs": [],
      "/api/v1/deliverables": [],
      "/api/v1/safemode/queue": [],
      "/api/v1/checkpoints": [],
      "/api/v1/workers": [{ id: "w1", name: "mac", status: "online", heartbeat_fresh: true }],
    }) as unknown as typeof fetch;

    const view = await getBrief();
    expect(view.hasLiveWorker).toBe(true);
  });
  /* ── needsYou completeness ────────────────────────────────────────────────
   * `listPendingDecisions` degrades each of its three queues to [] on a blip.
   * That keeps the Brief alive, but an EMPTY needs-you list is itself an answer
   * the founder acts on ("nothing is waiting on me") and the "Needs you"
   * section hides entirely when the list is empty. Measured 2026-09-03: with
   * /safemode/queue at 500 and a checkpoint present, the section rendered, the
   * chip said 1, and a held delivery awaiting approval was invisible with no
   * trace at all. So the read carries whether it was COMPLETE. */

  it("flags the needs-you list INCOMPLETE when one queue blips — and keeps what it did read", async () => {
    global.fetch = mockFetch({
      "/api/v1/products": [product("p1", "alpha", "alpha")],
      "/api/v1/runs": [{ ...run("r1", "p1", "running", "raw"), summary_title: "Export endpoint" }],
      "/api/v1/deliverables": [],
      "/api/v1/checkpoints": [
        {
          id: "cp-1",
          run_id: "r1",
          question: "Ship to prod or staging?",
          options: ["prod", "staging"],
          actions: null,
          decision: "ask_user_question",
          rationale: null,
          prior_decisions: [],
          created_at: NOW,
        },
      ],
      "/api/v1/decisions": [],
      // /api/v1/safemode/queue deliberately unmocked → 404 → its queue is lost.
    }) as unknown as typeof fetch;

    const view = await getBrief();
    expect(view.needsYouIncomplete).toBe(true);
    // The item that WAS read is not suppressed — an unknown is not a reason to
    // hide what we actually measured.
    expect(view.needsYou.map((n) => n.kind)).toEqual(["decision"]);
  });

  it("control: every queue answering makes the list COMPLETE", async () => {
    global.fetch = mockFetch({
      "/api/v1/products": [product("p1", "alpha", "alpha")],
      "/api/v1/runs": [],
      "/api/v1/deliverables": [],
      "/api/v1/safemode/queue": [],
      "/api/v1/checkpoints": [],
      "/api/v1/decisions": [],
    }) as unknown as typeof fetch;

    const view = await getBrief();
    expect(view.needsYouIncomplete).toBe(false);
    expect(view.needsYou).toEqual([]);
  });

  it("boundary: a failed review-CONTEXT read is not an incomplete list", async () => {
    // runs / deliverables / products only DECORATE a pending item (they supply
    // the task title + product beside the bare question). Losing them omits no
    // item, so it must not raise the incompleteness flag — otherwise the notice
    // fires on a degradation the founder cannot act on and stops meaning
    // "something is missing".
    global.fetch = mockFetch({
      "/api/v1/safemode/queue": [],
      "/api/v1/checkpoints": [],
      "/api/v1/decisions": [],
      "/api/v1/workers": [],
      // products / runs are core reads; deliverables is the optional context one.
      "/api/v1/products": [product("p1", "alpha", "alpha")],
      "/api/v1/runs": [],
      // /api/v1/deliverables unmocked → 404 → context lost, no item lost.
    }) as unknown as typeof fetch;

    const view = await getBrief();
    expect(view.needsYouIncomplete).toBe(false);
  });

  it("a hard failure of the whole aggregation is also INCOMPLETE, not 'nothing pending'", async () => {
    global.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      // Core reads succeed so the Brief does not fall to `placeholder`; only the
      // pending aggregation dies outright.
      if (url.includes("/api/v1/products")) {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/api/v1/runs")) {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("nope", { status: 500 });
    }) as unknown as typeof fetch;

    const view = await getBrief();
    expect(view.placeholder).toBe(false);
    expect(view.needsYouIncomplete).toBe(true);
  });
});
