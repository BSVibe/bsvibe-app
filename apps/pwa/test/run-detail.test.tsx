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
    // honest "Started directly by you." line, not a bare header.
    const trigger = screen.getByRole("region", { name: /trigger/i });
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
});
