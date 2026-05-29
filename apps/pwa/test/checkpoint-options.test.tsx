/**
 * B11a — paused-run checkpoint with structured options.
 *
 * Founder requirement: when the work LLM offered concrete choices via
 * ``ask_user_question`` with an ``options`` array (Workflow §5 #4), the
 * Decisions UI must render those as a single-select instead of a free-text
 * textarea, and the founder's pick is what gets POSTed to
 * ``/api/v1/checkpoints/{id}/resolve``. With no options, the existing
 * free-text path stays unchanged.
 *
 * Drives the real CheckpointRow component against a mocked fetch so the test
 * exercises the SAME wiring the live PWA uses (no fake glue).
 */

import CheckpointRow from "@/components/decisions/CheckpointRow";
import type { PendingCheckpoint } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const CHECKPOINT_WITH_OPTIONS: PendingCheckpoint = {
  kind: "decision",
  id: "checkpoint-aaa",
  checkpointId: "11111111-1111-1111-1111-111111111111",
  question: "Which database should I target?",
  rationale: null,
  options: ["postgres", "sqlite", "mysql"],
  actions: null,
  decision: "ask_user_question",
  priorDecisions: [],
  createdAt: "2026-05-26T10:00:00Z",
};

const CHECKPOINT_FREE_TEXT: PendingCheckpoint = {
  kind: "decision",
  id: "checkpoint-bbb",
  checkpointId: "22222222-2222-2222-2222-222222222222",
  question: "What should I do here?",
  rationale: null,
  options: null,
  actions: null,
  decision: "ask_user_question",
  priorDecisions: [],
  createdAt: "2026-05-26T10:00:00Z",
};

function okResolveResponse(checkpointId: string, answer: string): Response {
  return new Response(
    JSON.stringify({
      id: checkpointId,
      run_id: "run-1",
      status: "resolved",
      resolution: answer,
      resolved_at: "2026-05-26T10:05:00Z",
      run_status: "open",
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

describe("CheckpointRow — structured options (B11a)", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the offered options as a single-select control", async () => {
    vi.stubGlobal("fetch", vi.fn());
    render(<CheckpointRow item={CHECKPOINT_WITH_OPTIONS} onResolved={() => {}} />);

    // The question text is still surfaced.
    expect(screen.getByText("Which database should I target?")).toBeInTheDocument();
    // Each offered option renders as its own selectable control (radio).
    for (const opt of ["postgres", "sqlite", "mysql"]) {
      expect(screen.getByRole("radio", { name: opt })).toBeInTheDocument();
    }
    // The free-text textarea must NOT render in options mode.
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("posts the selected option to the resolve endpoint", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(init?.body as string) as { answer: string };
      return okResolveResponse(CHECKPOINT_WITH_OPTIONS.checkpointId, body.answer);
    });
    vi.stubGlobal("fetch", fetchMock);

    const onResolved = vi.fn();
    render(<CheckpointRow item={CHECKPOINT_WITH_OPTIONS} onResolved={onResolved} />);

    await userEvent.click(screen.getByRole("radio", { name: "sqlite" }));
    await userEvent.click(screen.getByRole("button", { name: /Answer|Resolve|Send/ }));

    await waitFor(() => expect(onResolved).toHaveBeenCalled());

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(
      `/api/v1/checkpoints/${CHECKPOINT_WITH_OPTIONS.checkpointId}/resolve`,
    );
    expect((init as RequestInit).method).toBe("POST");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ answer: "sqlite" });
  });

  it("keeps the submit button disabled until an option is selected", async () => {
    vi.stubGlobal("fetch", vi.fn());
    render(<CheckpointRow item={CHECKPOINT_WITH_OPTIONS} onResolved={() => {}} />);

    expect(screen.getByRole("button", { name: /Answer|Resolve|Send/ })).toBeDisabled();
    await userEvent.click(screen.getByRole("radio", { name: "postgres" }));
    expect(screen.getByRole("button", { name: /Answer|Resolve|Send/ })).not.toBeDisabled();
  });

  it("L-D1: offers an Other radio that reveals a free-text textarea", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(init?.body as string) as { answer: string };
      return okResolveResponse(CHECKPOINT_WITH_OPTIONS.checkpointId, body.answer);
    });
    vi.stubGlobal("fetch", fetchMock);

    const onResolved = vi.fn();
    render(<CheckpointRow item={CHECKPOINT_WITH_OPTIONS} onResolved={onResolved} />);

    // An "Other" radio renders alongside the LLM-supplied options.
    const otherRadio = screen.getByRole("radio", { name: "Other" });
    expect(otherRadio).toBeInTheDocument();

    // Before Other is picked, no textarea is shown (single-select feel intact).
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();

    await userEvent.click(otherRadio);

    // Picking Other reveals a free-text textarea; submit stays disabled until
    // the founder types something.
    const textarea = screen.getByRole("textbox");
    expect(textarea).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Answer|Resolve|Send/ })).toBeDisabled();

    await userEvent.type(textarea, "duckdb");
    await userEvent.click(screen.getByRole("button", { name: /Answer|Resolve|Send/ }));

    await waitFor(() => expect(onResolved).toHaveBeenCalled());
    const [, init] = fetchMock.mock.calls[0];
    // The verbatim off-list answer is POSTed (no membership coercion).
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ answer: "duckdb" });
  });

  it("falls back to a free-text textarea when no options are offered", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(init?.body as string) as { answer: string };
      return okResolveResponse(CHECKPOINT_FREE_TEXT.checkpointId, body.answer);
    });
    vi.stubGlobal("fetch", fetchMock);

    const onResolved = vi.fn();
    render(<CheckpointRow item={CHECKPOINT_FREE_TEXT} onResolved={onResolved} />);

    // No radios in free-text mode.
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    // The textarea is the input.
    const textarea = screen.getByRole("textbox");
    await userEvent.type(textarea, "ship it");
    await userEvent.click(screen.getByRole("button", { name: /Answer|Resolve|Send/ }));

    await waitFor(() => expect(onResolved).toHaveBeenCalled());
    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ answer: "ship it" });
  });

  it("surfaces similar prior decisions when present (G4)", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const withPrior: PendingCheckpoint = {
      ...CHECKPOINT_FREE_TEXT,
      priorDecisions: ["Prior decision — Q: Which database? A: Use Postgres"],
    };
    render(<CheckpointRow item={withPrior} onResolved={() => {}} />);
    expect(screen.getByText(/Use Postgres/)).toBeInTheDocument();
  });

  it("hides the prior-decisions section when none overlap (G4)", async () => {
    vi.stubGlobal("fetch", vi.fn());
    render(<CheckpointRow item={CHECKPOINT_FREE_TEXT} onResolved={() => {}} />);
    expect(screen.queryByText(/decided similar before/i)).toBeNull();
  });
});
