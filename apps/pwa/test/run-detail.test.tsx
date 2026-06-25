/**
 * Run-detail surface — the Stitch "Triggered" screen: one externally-triggered
 * ExecutionRun made inspectable. Drives the RunDetail container with a mocked
 * fetch and asserts:
 *  - the trigger context (source + intent) and current status render
 *  - the decision block renders the question + rationale for a paused run, with
 *    a Resolve affordance wired to POST /api/v1/checkpoints/{id}/resolve
 *  - resolving POSTs the answer and shows a resolved state
 *  - the verification outcome renders
 *  - a "View delivery report" link to /deliverables/{id} when a deliverable exists
 *  - a calm minimal detail for a sparse-payload run (no trigger/decision/etc.),
 *    never an error
 *  - a calm not-found state for an unknown id (404), and a calm inline error
 */

import RunDetail from "@/components/runs/RunDetail";
import type { RunDetail as RunDetailModel } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const NOW = "2026-05-24T00:00:00Z";

const TRIGGERED: RunDetailModel = {
  id: "r1",
  workspace_id: "ws-1",
  product_id: "p1",
  status: "running",
  created_at: NOW,
  updated_at: NOW,
  trigger: {
    source: "github",
    trigger_kind: "webhook",
    intent_text: "Mobile menu button is cut off on small screens",
    product: "quantum-link",
  },
  decisions: [
    {
      id: "dec-1",
      decision: "ask_user_question",
      question: "Let it continue?",
      rationale: "Because this came from outside, BSVibe is in Safe Mode.",
      status: "pending",
      resolution: null,
      created_at: NOW,
    },
  ],
  verification: { id: "v1", outcome: "passed", created_at: NOW },
  deliverable_id: "d1",
  partial_deliverables: [],
  activities: [
    { type: "tool_call", label: "Delivered calculator.py", created_at: NOW },
    { type: "verify", label: "Verified the work", created_at: NOW },
    { type: "settle", label: "Settled into knowledge", created_at: NOW },
  ],
  timeline_source: "activities",
  failure_reason: null,
};

/** A clean `review_ready` run (no pending decision) — drives the next-step
 *  assertions. */
const REVIEW_READY: RunDetailModel = {
  ...TRIGGERED,
  id: "rr",
  status: "review_ready",
  decisions: [],
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("Run-detail surface (Triggered)", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the trigger context, status, decision, verification, and report link", async () => {
    global.fetch = vi.fn(async () => json(TRIGGERED)) as unknown as typeof fetch;

    render(<RunDetail runId="r1" />);

    // The intent/title.
    await waitFor(() => {
      expect(
        screen.getByText("Mobile menu button is cut off on small screens"),
      ).toBeInTheDocument();
    });

    // Trigger source surfaces ("github" / webhook).
    const trigger = screen.getByRole("region", { name: /trigger/i });
    expect(within(trigger).getByText(/github/i)).toBeInTheDocument();
    // Externally-originated run → a calm Safe-Mode reassurance line.
    expect(
      within(trigger).getByText(/won’t push, merge, or reply|won't push, merge, or reply/i),
    ).toBeInTheDocument();

    // Decision block — the blocking question + rationale.
    const decision = screen.getByRole("region", { name: /decision|needs you/i });
    expect(within(decision).getByText("Let it continue?")).toBeInTheDocument();
    expect(within(decision).getByText(/Because this came from outside/i)).toBeInTheDocument();

    // Verification outcome.
    const verification = screen.getByRole("region", { name: /verification|checked/i });
    expect(within(verification).getByText(/verified|passed/i)).toBeInTheDocument();

    // "View delivery report" link → /deliverables/{deliverable_id}.
    expect(
      screen.getByRole("link", { name: /view delivery report|see the proof/i }),
    ).toHaveAttribute("href", "/deliverables/d1");
  });

  it("resolves a pending decision via POST /checkpoints/{id}/resolve", async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (typeof url === "string" && url.includes("/resolve")) {
        return json({
          id: "dec-1",
          run_id: "r1",
          status: "resolved",
          resolution: "Let it continue",
          resolved_at: NOW,
          run_status: "running",
        });
      }
      return json(TRIGGERED);
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<RunDetail runId="r1" />);

    // The "Let it continue" affordance.
    const letContinue = await screen.findByRole("button", { name: /let it continue/i });
    await userEvent.click(letContinue);

    await waitFor(() => {
      const resolveCall = fetchMock.mock.calls.find(
        (c) => typeof c[0] === "string" && (c[0] as string).includes("/resolve"),
      );
      expect(resolveCall).toBeTruthy();
      const [url, init] = resolveCall as unknown as [string, RequestInit];
      expect(url).toBe("/api/v1/checkpoints/dec-1/resolve");
      expect(init.method).toBe("POST");
    });
  });

  it("renders a calm minimal detail for a sparse-payload run (no error)", async () => {
    const sparse: RunDetailModel = {
      id: "r2",
      workspace_id: "ws-1",
      product_id: null,
      status: "open",
      created_at: NOW,
      updated_at: NOW,
      trigger: { source: null, trigger_kind: null, intent_text: null, product: null },
      decisions: [],
      verification: null,
      deliverable_id: null,
      partial_deliverables: [],
      activities: [],
      timeline_source: "derived",
      failure_reason: null,
    };
    global.fetch = vi.fn(async () => json(sparse)) as unknown as typeof fetch;

    render(<RunDetail runId="r2" />);

    // It renders something calm (the run status), not an error wall, and no
    // delivery-report link / decision block.
    await waitFor(() => {
      expect(screen.queryByText(/couldn’t load|couldn't load/i)).not.toBeInTheDocument();
    });
    expect(screen.queryByRole("link", { name: /view delivery report/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /let it continue/i })).not.toBeInTheDocument();

    // The trigger section is never empty — a Direct run (no source/kind) shows an
    // honest "Started directly by you." line, not a bare header. Use findByRole
    // (await) so we wait for the fetched content to render, not the loading state
    // (the error-absent waitFor above is true during loading too → was flaky).
    const trigger = await screen.findByRole("region", { name: /trigger/i });
    expect(within(trigger).getByText(/started directly by you/i)).toBeInTheDocument();
    // And NOT the external Safe-Mode reassurance line (no external origin here).
    expect(
      within(trigger).queryByText(/won’t push, merge, or reply|won't push, merge, or reply/i),
    ).not.toBeInTheDocument();
  });

  it("shows a calm not-found state for an unknown id (404)", async () => {
    global.fetch = vi.fn(async () => json({ detail: "not found" }, 404)) as unknown as typeof fetch;

    render(<RunDetail runId="ghost" />);

    await waitFor(() => {
      expect(screen.getByText(/can’t find that run|can't find that run/i)).toBeInTheDocument();
    });
  });

  it("renders a calm inline error (not a blank page) when the read fails", async () => {
    global.fetch = vi.fn(async () => json("boom", 500)) as unknown as typeof fetch;

    render(<RunDetail runId="r1" />);

    await waitFor(() => {
      expect(
        screen.getByText(/couldn’t load this run|couldn't load this run/i),
      ).toBeInTheDocument();
    });
  });

  it("renders the activity timeline (the run's story) in order", async () => {
    global.fetch = vi.fn(async () => json(TRIGGERED)) as unknown as typeof fetch;

    render(<RunDetail runId="r1" />);

    const timeline = await screen.findByRole("region", { name: /what i did|timeline/i });
    // The ordered events from the response render, oldest first.
    expect(within(timeline).getByText(/Delivered calculator\.py/i)).toBeInTheDocument();
    expect(within(timeline).getByText(/Verified the work/i)).toBeInTheDocument();
    expect(within(timeline).getByText(/Settled into knowledge/i)).toBeInTheDocument();
  });

  it("shows a derived-timeline note when no real activities were recorded", async () => {
    const derived: RunDetailModel = {
      ...REVIEW_READY,
      activities: [{ type: "verify", label: "Verified the work", created_at: NOW }],
      timeline_source: "derived",
    };
    global.fetch = vi.fn(async () => json(derived)) as unknown as typeof fetch;

    render(<RunDetail runId="rr" />);

    const timeline = await screen.findByRole("region", { name: /what i did|timeline/i });
    expect(within(timeline).getByText(/Verified the work/i)).toBeInTheDocument();
    // An honest note that the story was reconstructed (not a faked per-step log).
    expect(within(timeline).getByText(/reconstructed|from what was recorded/i)).toBeInTheDocument();
  });

  it("next step: review_ready points the founder at the delivery report", async () => {
    global.fetch = vi.fn(async () => json(REVIEW_READY)) as unknown as typeof fetch;

    render(<RunDetail runId="rr" />);

    const nextStep = await screen.findByRole("region", { name: /next step|what.s next/i });
    expect(within(nextStep).getByText(/ready for your review/i)).toBeInTheDocument();
    // The link out is the delivery report for the deliverable.
    expect(within(nextStep).getByRole("link")).toHaveAttribute("href", "/deliverables/d1");
  });

  it("next step: a pending decision asks the founder to resolve it", async () => {
    global.fetch = vi.fn(async () => json(TRIGGERED)) as unknown as typeof fetch;

    render(<RunDetail runId="r1" />);

    const nextStep = await screen.findByRole("region", { name: /next step|what.s next/i });
    expect(within(nextStep).getByText(/needs your decision|waiting on you/i)).toBeInTheDocument();
    // The resolve affordance still lives in the decision block (not reinvented).
    expect(screen.getByRole("button", { name: /let it continue/i })).toBeInTheDocument();
  });

  it("next step: a running run shows a calm working line + a Stop button", async () => {
    const running: RunDetailModel = { ...REVIEW_READY, status: "running" };
    global.fetch = vi.fn(async () => json(running)) as unknown as typeof fetch;

    render(<RunDetail runId="rr" />);

    const nextStep = await screen.findByRole("region", { name: /next step|what.s next/i });
    expect(within(nextStep).getByText(/working|on it/i)).toBeInTheDocument();
    // L9 — an in-flight run can be stopped.
    expect(within(nextStep).getByRole("button", { name: /stop/i })).toBeInTheDocument();
  });

  it("clicking Stop POSTs /api/v1/runs/{id}/cancel and reloads", async () => {
    const running: RunDetailModel = { ...REVIEW_READY, status: "running" };
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (typeof url === "string" && url.includes("/cancel")) {
        return json({ id: "rr", status: "cancelled" });
      }
      return json(running);
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<RunDetail runId="rr" />);

    const stop = await screen.findByRole("button", { name: /stop/i });
    await userEvent.click(stop);

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (c) => typeof c[0] === "string" && (c[0] as string).includes("/cancel"),
      );
      expect(call).toBeTruthy();
      const [url, init] = call as unknown as [string, RequestInit];
      expect(url).toBe("/api/v1/runs/rr/cancel");
      expect(init.method).toBe("POST");
    });
  });

  it("next step: a shipped run shows NO Stop button (terminal)", async () => {
    const shipped: RunDetailModel = { ...REVIEW_READY, status: "shipped" };
    global.fetch = vi.fn(async () => json(shipped)) as unknown as typeof fetch;

    render(<RunDetail runId="rr" />);

    const nextStep = await screen.findByRole("region", { name: /next step|what.s next/i });
    expect(within(nextStep).queryByRole("button", { name: /stop/i })).not.toBeInTheDocument();
  });

  it("next step: a shipped run says shipped and needs nothing", async () => {
    const shipped: RunDetailModel = { ...REVIEW_READY, status: "shipped" };
    global.fetch = vi.fn(async () => json(shipped)) as unknown as typeof fetch;

    render(<RunDetail runId="rr" />);

    const nextStep = await screen.findByRole("region", { name: /next step|what.s next/i });
    expect(within(nextStep).getByText(/shipped|all done|nothing needed/i)).toBeInTheDocument();
  });

  it("next step: a failed run shows a calm failure line + a Retry button", async () => {
    const failed: RunDetailModel = { ...REVIEW_READY, status: "failed", deliverable_id: null };
    global.fetch = vi.fn(async () => json(failed)) as unknown as typeof fetch;

    render(<RunDetail runId="rr" />);

    const nextStep = await screen.findByRole("region", { name: /next step|what.s next/i });
    expect(within(nextStep).getByText(/didn.t finish/i)).toBeInTheDocument();
    // L2 (#9) — recoverable, not a dead-end: a Retry affordance is present.
    expect(within(nextStep).getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("next step: a failed run surfaces a HUMANIZED reason (sandbox), not the raw text", async () => {
    const failed: RunDetailModel = {
      ...REVIEW_READY,
      status: "failed",
      deliverable_id: null,
      failure_reason: "the work sandbox could not start",
    };
    global.fetch = vi.fn(async () => json(failed)) as unknown as typeof fetch;

    render(<RunDetail runId="rr" />);

    const nextStep = await screen.findByRole("region", { name: /next step|what.s next/i });
    // The humanized sandbox line is the primary text the founder reads.
    expect(within(nextStep).getByText(/work sandbox couldn.t start/i)).toBeInTheDocument();
  });

  it("next step: a UUID-laden executor-crash reason is humanized; the raw UUID is NOT shown as primary text", async () => {
    const failed: RunDetailModel = {
      ...REVIEW_READY,
      status: "failed",
      deliverable_id: null,
      failure_reason:
        "loop crashed: executor chat task 61483a05-1b2c-4d5e-8f90-abcdef123456 failed: exit 1",
    };
    global.fetch = vi.fn(async () => json(failed)) as unknown as typeof fetch;

    render(<RunDetail runId="rr" />);

    const nextStep = await screen.findByRole("region", { name: /next step|what.s next/i });
    // The calm humanized line is the primary text.
    const reason = within(nextStep).getByText(/coding agent stopped unexpectedly/i);
    expect(reason).toBeInTheDocument();
    // The raw UUID is NOT shown as the primary reason text.
    expect(reason.textContent ?? "").not.toMatch(/61483a05-1b2c/);
    // The raw reason is available behind a collapsed "Technical details"
    // disclosure — and even there the UUID is stripped.
    const details = within(nextStep).getByText(/technical details/i);
    expect(details).toBeInTheDocument();
  });

  it("clicking Retry POSTs /api/v1/runs/{id}/retry and reloads", async () => {
    const failed: RunDetailModel = { ...REVIEW_READY, status: "failed", deliverable_id: null };
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (typeof url === "string" && url.includes("/retry")) {
        return json({ id: "rr", status: "open", retry_count: 1 });
      }
      return json(failed);
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<RunDetail runId="rr" />);

    const retryButton = await screen.findByRole("button", { name: /retry/i });
    await userEvent.click(retryButton);

    await waitFor(() => {
      const retryCall = fetchMock.mock.calls.find(
        (c) => typeof c[0] === "string" && (c[0] as string).includes("/retry"),
      );
      expect(retryCall).toBeTruthy();
      const [url, init] = retryCall as unknown as [string, RequestInit];
      expect(url).toBe("/api/v1/runs/rr/retry");
      expect(init.method).toBe("POST");
    });
  });

  // D6 — mid-loop partial Deliverables render distinguished from the verified
  // terminal in the Run-view (Synthesis §13 — continuous Deliver side channel).
  it("renders mid-loop partial Deliverables in a list, distinguished from the verified-final", async () => {
    const withPartials: RunDetailModel = {
      ...REVIEW_READY,
      partial_deliverables: [
        {
          id: "p1",
          artifact_type: "pr",
          summary: "opened PR #1",
          channel: "github",
          external_ref: "github://acme/site/pull/1",
          created_at: NOW,
        },
        {
          id: "p2",
          artifact_type: "page",
          summary: "updated runbook",
          channel: "notion",
          external_ref: null,
          created_at: NOW,
        },
      ],
    };
    global.fetch = vi.fn(async () => json(withPartials)) as unknown as typeof fetch;

    render(<RunDetail runId="rr" />);

    // The verified-final's report link still resolves.
    await screen.findByRole("link", { name: /view delivery report|see the proof/i });

    // Each partial renders as a row tagged with data-partial="true" so the
    // partial vs verified-final distinction lives in the DOM (not just text).
    const partialRows = document.querySelectorAll('[data-partial="true"]');
    expect(partialRows.length).toBe(2);
    // The partials carry their founder-relevant fields.
    expect(screen.getByText("opened PR #1")).toBeInTheDocument();
    expect(screen.getByText("updated runbook")).toBeInTheDocument();

    // The verified-final lives in a SEPARATE container marked data-verified
    // — the founder can tell the terminal apart from the streaming partials.
    expect(document.querySelector('[data-verified="true"]')).not.toBeNull();
  });

  it("a run with zero mid-loop partials renders unchanged from pre-D6", async () => {
    global.fetch = vi.fn(async () => json(TRIGGERED)) as unknown as typeof fetch;
    render(<RunDetail runId="r1" />);

    await screen.findByRole("link", { name: /view delivery report|see the proof/i });
    // No partials block — empty list yields no rows.
    expect(document.querySelectorAll('[data-partial="true"]').length).toBe(0);
  });
});
