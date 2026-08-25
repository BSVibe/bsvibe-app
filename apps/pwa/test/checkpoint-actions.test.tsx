/**
 * L-D2 — paused-run checkpoint with one-click actions (ship / discard).
 *
 * Executor B2b Decisions (verification_failed / human_review_required) carry
 * structured `actions` with localized labels. CheckpointRow must render
 * those as dedicated buttons that POST `{ action_key }` (not free-text)
 * and trigger the side-effecting backend handlers.
 */

import CheckpointRow from "@/components/decisions/CheckpointRow";
import type { CheckpointAction, PendingCheckpoint } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import koMessages from "@/messages/ko.json";
// The shim wraps every render in an `en` provider; reach the real render for
// the explicit `ko` provider the localized-label proof needs.
import { render as renderActual } from "@rtl-actual";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

function renderKo(ui: ReactElement) {
  return renderActual(
    <NextIntlClientProvider locale="ko" messages={koMessages}>
      {ui}
    </NextIntlClientProvider>,
  );
}

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const ACTIONS: CheckpointAction[] = [
  { key: "ship", label_en: "Approve & ship", label_ko: "승인하고 출시" },
  { key: "discard", label_en: "Discard", label_ko: "폐기" },
];

/** PR7 — the AMBIGUOUS-merge-conflict Decision. Its first action is literally
 *  labelled "Guide & retry" / "지침 주고 다시 시도", so the card MUST offer a
 *  place to write that guidance and MUST send it with the click. */
const MERGE_CONFLICT_ACTIONS: CheckpointAction[] = [
  { key: "retry", label_en: "Guide & retry", label_ko: "지침 주고 다시 시도" },
  { key: "discard", label_en: "Discard", label_ko: "폐기" },
];

const VERIFICATION_FAILED: PendingCheckpoint = {
  kind: "decision",
  id: "checkpoint-vf",
  checkpointId: "33333333-3333-3333-3333-333333333333",
  question: "BSVibe couldn't verify this work — review it before it ships?",
  rationale: null,
  options: null,
  actions: ACTIONS,
  decision: "verification_failed",
  priorDecisions: [],
  createdAt: "2026-05-27T10:00:00Z",
};

const MERGE_CONFLICT_REVIEW: PendingCheckpoint = {
  kind: "decision",
  id: "checkpoint-mc",
  checkpointId: "44444444-4444-4444-4444-444444444444",
  question: "Two changes touched the same logic — how should it resolve?",
  rationale: null,
  options: null,
  actions: MERGE_CONFLICT_ACTIONS,
  decision: "merge_conflict_review",
  priorDecisions: [],
  createdAt: "2026-08-25T10:00:00Z",
};

const GUIDANCE = "Keep our branch's formula and drop theirs";

function okResolveResponse(checkpointId: string, resolution: string, runStatus: string): Response {
  return new Response(
    JSON.stringify({
      id: checkpointId,
      run_id: "run-1",
      status: "resolved",
      resolution,
      resolved_at: "2026-05-27T10:05:00Z",
      run_status: runStatus,
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

describe("CheckpointRow — one-click actions (L-D2)", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders both ship and discard buttons with English labels", () => {
    vi.stubGlobal("fetch", vi.fn());
    render(<CheckpointRow item={VERIFICATION_FAILED} onResolved={() => {}} />);

    expect(screen.getByRole("button", { name: "Approve & ship" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Discard" })).toBeInTheDocument();
  });

  it("renders the agent-authored question and rationale as markdown, not raw syntax", () => {
    // The question/rationale are LLM-authored prose the founder reads to decide;
    // they can carry markdown (options as a list, `code` refs, **emphasis**).
    vi.stubGlobal("fetch", vi.fn());
    const item: PendingCheckpoint = {
      ...VERIFICATION_FAILED,
      title: "Ship the clamp helper?",
      question: "Which bound wins when **equal**?\n\n- `low`\n- `high`",
      rationale: "Because `clamp` must be **total**.",
    };
    const { container } = render(<CheckpointRow item={item} onResolved={() => {}} />);

    expect(container.querySelector(".need-card__body strong")?.textContent).toBe("equal");
    expect(container.querySelectorAll(".need-card__body li")).toHaveLength(2);
    expect(container.querySelector(".need-card__body code")?.textContent).toBe("low");
    expect(container.querySelector(".need-card__rationale strong")?.textContent).toBe("total");
    expect(container.querySelector(".need-card__body")?.textContent).not.toContain("**");
  });

  it("clicking Approve & ship POSTs { action_key: 'ship' } to resolve", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      return okResolveResponse(VERIFICATION_FAILED.checkpointId, "ship", "shipped");
    });
    vi.stubGlobal("fetch", fetchMock);

    const onResolved = vi.fn();
    render(<CheckpointRow item={VERIFICATION_FAILED} onResolved={onResolved} />);

    await userEvent.click(screen.getByRole("button", { name: "Approve & ship" }));
    await waitFor(() => expect(onResolved).toHaveBeenCalled());

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(
      `/api/v1/checkpoints/${VERIFICATION_FAILED.checkpointId}/resolve`,
    );
    expect((init as RequestInit).method).toBe("POST");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ action_key: "ship" });
  });

  it("clicking Discard POSTs { action_key: 'discard' } to resolve", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      return okResolveResponse(VERIFICATION_FAILED.checkpointId, "discard", "cancelled");
    });
    vi.stubGlobal("fetch", fetchMock);

    const onResolved = vi.fn();
    render(<CheckpointRow item={VERIFICATION_FAILED} onResolved={onResolved} />);

    await userEvent.click(screen.getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(onResolved).toHaveBeenCalled());

    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ action_key: "discard" });
  });

  it("keeps the free-text-only path working alongside the actions", async () => {
    // Positive control (d): answering with words alone — no action — still
    // POSTs `{ answer }` and resumes the run. The guidance box doubles as that
    // input, so widening it must not take the plain path away.
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      return okResolveResponse(VERIFICATION_FAILED.checkpointId, "needs more eyes", "open");
    });
    vi.stubGlobal("fetch", fetchMock);

    const onResolved = vi.fn();
    render(<CheckpointRow item={VERIFICATION_FAILED} onResolved={onResolved} />);

    const box = screen.getByRole("textbox");
    await userEvent.type(box, "needs more eyes");
    await userEvent.click(screen.getByRole("button", { name: /Answer|Resolve|Send/ }));

    await waitFor(() => expect(onResolved).toHaveBeenCalled());
    const [, init] = fetchMock.mock.calls[0];
    // Free-text path goes through the answer field — NOT action_key.
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      answer: "needs more eyes",
    });
  });
});

/**
 * §14 — the button says "Guide & retry", so the guidance has to leave the
 * browser. Before this, `submitAction` posted `{ action_key }` and the typed
 * text was dropped client-side: the resuming agent read `A: retry` where the
 * founder's words belonged.
 */
describe("CheckpointRow — an action carries the founder's guidance (§14)", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("offers a guidance box next to the actions without a disclosure to find", () => {
    vi.stubGlobal("fetch", vi.fn());
    render(<CheckpointRow item={MERGE_CONFLICT_REVIEW} onResolved={() => {}} />);

    expect(screen.getByRole("button", { name: "Guide & retry" })).toBeInTheDocument();
    // The box is visible from the start: a button that promises guidance can't
    // hide the only place to write it behind an "Other" toggle.
    expect(screen.getByRole("textbox")).toBeInTheDocument();
  });

  it("sends the typed guidance WITH the action click", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => {
      return okResolveResponse(MERGE_CONFLICT_REVIEW.checkpointId, "retry", "open");
    });
    vi.stubGlobal("fetch", fetchMock);

    const onResolved = vi.fn();
    render(<CheckpointRow item={MERGE_CONFLICT_REVIEW} onResolved={onResolved} />);

    await userEvent.type(screen.getByRole("textbox"), GUIDANCE);
    await userEvent.click(screen.getByRole("button", { name: "Guide & retry" }));

    await waitFor(() => expect(onResolved).toHaveBeenCalled());
    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      action_key: "retry",
      reason: GUIDANCE,
    });
  });

  it("sends a discard's reason too — the PWA could not reach negative knowledge before", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => {
      return okResolveResponse(MERGE_CONFLICT_REVIEW.checkpointId, "discard", "cancelled");
    });
    vi.stubGlobal("fetch", fetchMock);

    const onResolved = vi.fn();
    render(<CheckpointRow item={MERGE_CONFLICT_REVIEW} onResolved={onResolved} />);

    await userEvent.type(screen.getByRole("textbox"), "duplicates work we already shipped");
    await userEvent.click(screen.getByRole("button", { name: "Discard" }));

    await waitFor(() => expect(onResolved).toHaveBeenCalled());
    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      action_key: "discard",
      reason: "duplicates work we already shipped",
    });
  });

  it("sends action_key alone when the founder typed nothing", async () => {
    // Positive controls (a)/(b) start here: a text-free action must stay
    // text-free on the wire, or #823's suppression can never fire again.
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => {
      return okResolveResponse(MERGE_CONFLICT_REVIEW.checkpointId, "retry", "open");
    });
    vi.stubGlobal("fetch", fetchMock);

    const onResolved = vi.fn();
    render(<CheckpointRow item={MERGE_CONFLICT_REVIEW} onResolved={onResolved} />);

    await userEvent.type(screen.getByRole("textbox"), "   ");
    await userEvent.click(screen.getByRole("button", { name: "Guide & retry" }));

    await waitFor(() => expect(onResolved).toHaveBeenCalled());
    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ action_key: "retry" });
  });

  it("renders the Korean guidance label under the ko locale", () => {
    vi.stubGlobal("fetch", vi.fn());
    renderKo(<CheckpointRow item={MERGE_CONFLICT_REVIEW} onResolved={() => {}} />);

    expect(screen.getByRole("button", { name: "지침 주고 다시 시도" })).toBeInTheDocument();
    expect(screen.getByLabelText(koMessages.decisions.guidanceLabel)).toBeInTheDocument();
  });
});
