/**
 * InspectorActions — the founder-facing write surface that wires the modals,
 * the network calls, and the 30-second undo toast together (Lift M3b).
 * Asserts:
 *  - clicking Retract opens the retract modal; confirming POSTs to
 *    /api/v1/inside/nodes/{ref}/retract and surfaces the undo toast
 *  - the toast countdown derives from `signal.apply_at` (server-stamped wall
 *    clock); the toast renders with the right message
 *  - clicking Undo POSTs to /api/v1/inside/corrections/{id}/undo and flips the
 *    toast to "Restored." on `status=undone`
 *  - on `status=expired` the toast flips to "Undo window expired." + the
 *    parent's `onWindowClosed` hook fires
 *  - on the auto-expiry timer (no Undo click), the toast flips to "Undo window
 *    expired." + `onWindowClosed` fires with finalState=applied
 *  - the Correct button is DISABLED (the in-place field-rewrite editor was
 *    never built; the backend refuses it) — clicking it opens no modal, makes
 *    no network call, and never shows a false "Corrected" toast
 *  - onApplied(action) fires exactly once when the toast countdown starts
 */

import InspectorActions from "@/components/knowledge/InspectorActions";
import { setSession } from "@/lib/auth/session";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

function clickModalConfirm(name: string) {
  // Find the confirm button INSIDE the open dialog so we don't grab the
  // inspector-panel button that shares the accessible name.
  const dialog = screen.getByRole("dialog");
  fireEvent.click(within(dialog).getByRole("button", { name }));
}

function makeSignal(overrides: Record<string, unknown> = {}) {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    workspace_id: "22222222-2222-2222-2222-222222222222",
    actor_id: "33333333-3333-3333-3333-333333333333",
    node_ref: "garden/seedling/rate-limit.md",
    action: "retract",
    issued_at: "2026-05-30T12:00:00Z",
    apply_at: "2026-05-30T12:00:30Z",
    reason: null,
    source: "ontology_inspect_ui",
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("InspectorActions", () => {
  beforeEach(() => {
    setSession({
      accessToken: "tok",
      refreshToken: "r",
      email: "f@bsvibe.dev",
      userId: "u",
      expiresAt: Date.now() + 3_600_000,
    });
    // `shouldAdvanceTime: true` lets `waitFor` (which uses setTimeout) tick
    // alongside the simulated wall clock — without it, the test framework's
    // own polling deadlocks against our frozen timer.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-05-30T12:00:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("clicking Retract opens the modal; confirming POSTs + opens the undo toast", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ signal: makeSignal(), created: true, undo_window_seconds: 30 }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;
    const onApplied = vi.fn();

    render(
      <InspectorActions
        nodeRef="garden/seedling/rate-limit.md"
        nodeName="rate-limit"
        onApplied={onApplied}
      />,
    );

    fireEvent.click(screen.getByTestId("inspector-action-retract"));

    expect(screen.getByRole("dialog")).toBeInTheDocument();

    await act(async () => {
      clickModalConfirm("Retract");
    });

    // Network call happened with the right URL + method.
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/inside/nodes/garden/seedling/rate-limit.md/retract");
    expect((init.method ?? "GET").toUpperCase()).toBe("POST");

    // Undo toast appeared with the right message + countdown.
    await waitFor(() => expect(screen.getByTestId("undo-toast")).toBeInTheDocument());
    expect(
      screen.getByText(/Retracted "rate-limit"\. Future runs won't see this\./),
    ).toBeInTheDocument();
    expect(screen.getByTestId("undo-toast-undo")).toHaveTextContent("Undo (30s)");

    // onApplied fired exactly once with "retract".
    expect(onApplied).toHaveBeenCalledTimes(1);
    expect(onApplied).toHaveBeenCalledWith("retract");
  });

  it("clicking Undo POSTs to the undo endpoint + flips the toast to Restored", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ signal: makeSignal(), created: true, undo_window_seconds: 30 }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          correction_id: "11111111-1111-1111-1111-111111111111",
          status: "undone",
        }),
      );
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<InspectorActions nodeRef="garden/seedling/foo.md" nodeName="rate-limit" />);

    fireEvent.click(screen.getByTestId("inspector-action-retract"));
    await act(async () => {
      clickModalConfirm("Retract");
    });

    await waitFor(() => expect(screen.getByTestId("undo-toast")).toBeInTheDocument());

    await act(async () => {
      fireEvent.click(screen.getByTestId("undo-toast-undo"));
    });

    // Undo endpoint posted.
    const undoCall = fetchMock.mock.calls.find(([u]) => String(u).includes("/corrections/"));
    expect(undoCall).toBeDefined();
    expect(undoCall?.[0]).toBe(
      "/api/v1/inside/corrections/11111111-1111-1111-1111-111111111111/undo",
    );

    // Toast flipped to "Restored."
    await waitFor(() => expect(screen.getByText("Restored.")).toBeInTheDocument());
  });

  it("on `expired` status the toast flips + onWindowClosed fires with expired", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ signal: makeSignal(), created: true, undo_window_seconds: 30 }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          correction_id: "11111111-1111-1111-1111-111111111111",
          status: "expired",
        }),
      );
    global.fetch = fetchMock as unknown as typeof fetch;
    const onWindowClosed = vi.fn();

    render(
      <InspectorActions
        nodeRef="garden/seedling/foo.md"
        nodeName="rate-limit"
        onWindowClosed={onWindowClosed}
      />,
    );

    fireEvent.click(screen.getByTestId("inspector-action-retract"));
    await act(async () => {
      clickModalConfirm("Retract");
    });
    await waitFor(() => expect(screen.getByTestId("undo-toast")).toBeInTheDocument());

    await act(async () => {
      fireEvent.click(screen.getByTestId("undo-toast-undo"));
    });

    await waitFor(() => expect(screen.getByText("Undo window expired.")).toBeInTheDocument());
    expect(onWindowClosed).toHaveBeenCalledWith("retract", "expired");
  });

  it("the auto-expiry timer flips the toast + fires onWindowClosed=applied", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ signal: makeSignal(), created: true, undo_window_seconds: 30 }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;
    const onWindowClosed = vi.fn();

    render(
      <InspectorActions
        nodeRef="garden/seedling/foo.md"
        nodeName="rate-limit"
        onWindowClosed={onWindowClosed}
      />,
    );

    fireEvent.click(screen.getByTestId("inspector-action-retract"));
    await act(async () => {
      clickModalConfirm("Retract");
    });
    await waitFor(() => expect(screen.getByTestId("undo-toast")).toBeInTheDocument());

    // Advance past the 30s deadline (apply_at = T+30s).
    await act(async () => {
      vi.advanceTimersByTime(31000);
    });

    expect(screen.getByText("Undo window expired.")).toBeInTheDocument();
    expect(onWindowClosed).toHaveBeenCalledWith("retract", "applied");
  });

  it("the Correct button is disabled — no modal, no POST, no false 'Corrected' toast", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ signal: makeSignal(), created: true, undo_window_seconds: 30 }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;
    const onApplied = vi.fn();

    render(
      <InspectorActions
        nodeRef="garden/seedling/foo.md"
        nodeName="rate-limit"
        onApplied={onApplied}
      />,
    );

    const correctButton = screen.getByTestId("inspector-action-correct");
    expect(correctButton).toBeDisabled();

    // Clicking a disabled control must do nothing: no modal opens, no network
    // call fires, no toast appears, onApplied never fires.
    fireEvent.click(correctButton);

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByTestId("undo-toast")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(onApplied).not.toHaveBeenCalled();
  });
});
