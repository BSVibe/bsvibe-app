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

const ACTIONS: CheckpointAction[] = [
  { key: "ship", label_en: "Approve & ship", label_ko: "승인하고 출시" },
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
  createdAt: "2026-05-27T10:00:00Z",
};

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

  it("offers a free-text disclosure alongside the actions", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      return okResolveResponse(VERIFICATION_FAILED.checkpointId, "needs more eyes", "open");
    });
    vi.stubGlobal("fetch", fetchMock);

    const onResolved = vi.fn();
    render(<CheckpointRow item={VERIFICATION_FAILED} onResolved={onResolved} />);

    // No textarea before the disclosure is opened.
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();

    // The "Other" toggle opens a free-text textarea.
    await userEvent.click(screen.getByRole("button", { name: "Other" }));
    const textarea = screen.getByRole("textbox");
    expect(textarea).toBeInTheDocument();

    await userEvent.type(textarea, "needs more eyes");
    await userEvent.click(screen.getByRole("button", { name: /Answer|Resolve|Send/ }));

    await waitFor(() => expect(onResolved).toHaveBeenCalled());
    const [, init] = fetchMock.mock.calls[0];
    // Free-text path goes through the answer field — NOT action_key.
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      answer: "needs more eyes",
    });
  });
});
