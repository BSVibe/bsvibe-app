/**
 * UndoToast — the calm 30-second undo affordance (Lift M3b). Asserts:
 *  - the countdown derives the remainder from `apply_at - now()` (wall-clock,
 *    not naive decrement) and re-renders the seconds each tick
 *  - the `onExpired` callback fires exactly ONCE when the remainder hits zero
 *    (NOT every tick after, NOT before)
 *  - clicking the Undo button calls `onUndo` while in countdown; the parent's
 *    `state` prop change to `undoing`/`undone`/`expired` flips the toast
 *    rendering to the terminal copy
 *  - terminal states ("undone" / "expired" / "error") render the dismiss `×`
 *    and clicking it calls `onDismiss`
 *  - `remainingSeconds` clamps below-zero deadlines to 0 (a stale toast that
 *    rendered after the deadline shouldn't show a negative countdown)
 */

import UndoToast, { remainingSeconds } from "@/components/knowledge/UndoToast";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("remainingSeconds", () => {
  it("returns the whole-second remainder between now and the deadline", () => {
    const now = Date.parse("2026-05-30T12:00:00Z");
    const deadline = "2026-05-30T12:00:25Z";
    expect(remainingSeconds(deadline, now)).toBe(25);
  });

  it("clamps a past deadline to zero (no negative countdown)", () => {
    const now = Date.parse("2026-05-30T12:00:30Z");
    const deadline = "2026-05-30T11:59:55Z";
    expect(remainingSeconds(deadline, now)).toBe(0);
  });

  it("returns zero for an unparseable apply_at (defensive)", () => {
    expect(remainingSeconds("not-a-date", Date.now())).toBe(0);
  });
});

describe("UndoToast", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-05-30T12:00:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders the message + the live countdown in `countdown` state", () => {
    render(
      <UndoToast
        message='Retracted "rate-limit"'
        applyAt="2026-05-30T12:00:30Z"
        state={{ status: "countdown" }}
        onUndo={() => {}}
        onExpired={() => {}}
        onDismiss={() => {}}
      />,
    );

    expect(screen.getByText('Retracted "rate-limit"')).toBeInTheDocument();
    // Initial render — 30s remain.
    expect(screen.getByTestId("undo-toast-undo")).toHaveTextContent("Undo (30s)");
  });

  it("re-derives the countdown from wall-clock each tick", () => {
    render(
      <UndoToast
        message='Retracted "rate-limit"'
        applyAt="2026-05-30T12:00:30Z"
        state={{ status: "countdown" }}
        onUndo={() => {}}
        onExpired={() => {}}
        onDismiss={() => {}}
      />,
    );

    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(screen.getByTestId("undo-toast-undo")).toHaveTextContent("Undo (25s)");

    act(() => {
      vi.advanceTimersByTime(10000);
    });
    expect(screen.getByTestId("undo-toast-undo")).toHaveTextContent("Undo (15s)");
  });

  it("fires onExpired exactly once when the deadline crosses zero", () => {
    const onExpired = vi.fn();
    render(
      <UndoToast
        message='Retracted "rate-limit"'
        applyAt="2026-05-30T12:00:05Z"
        state={{ status: "countdown" }}
        onUndo={() => {}}
        onExpired={onExpired}
        onDismiss={() => {}}
      />,
    );

    act(() => {
      // Advance past the deadline.
      vi.advanceTimersByTime(7000);
    });

    expect(onExpired).toHaveBeenCalledTimes(1);

    // Further ticks must NOT re-fire (the interval is cleared on expiry).
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(onExpired).toHaveBeenCalledTimes(1);
  });

  it("clicking Undo calls onUndo while in countdown", () => {
    const onUndo = vi.fn();
    render(
      <UndoToast
        message='Retracted "rate-limit"'
        applyAt="2026-05-30T12:00:30Z"
        state={{ status: "countdown" }}
        onUndo={onUndo}
        onExpired={() => {}}
        onDismiss={() => {}}
      />,
    );

    fireEvent.click(screen.getByTestId("undo-toast-undo"));
    expect(onUndo).toHaveBeenCalledTimes(1);
  });

  it("renders the `undone` terminal copy + dismiss button", () => {
    const onDismiss = vi.fn();
    render(
      <UndoToast
        message='Retracted "rate-limit"'
        applyAt="2026-05-30T12:00:30Z"
        state={{ status: "undone" }}
        onUndo={() => {}}
        onExpired={() => {}}
        onDismiss={onDismiss}
      />,
    );

    expect(screen.getByText("Restored.")).toBeInTheDocument();
    expect(screen.queryByTestId("undo-toast-undo")).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("Dismiss"));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("renders the `expired` terminal copy", () => {
    render(
      <UndoToast
        message='Retracted "rate-limit"'
        applyAt="2026-05-30T12:00:30Z"
        state={{ status: "expired" }}
        onUndo={() => {}}
        onExpired={() => {}}
        onDismiss={() => {}}
      />,
    );

    expect(screen.getByText("Undo window expired.")).toBeInTheDocument();
    expect(screen.queryByTestId("undo-toast-undo")).not.toBeInTheDocument();
  });

  it("renders the disabled `Undoing…` label while the undo POST is in flight", () => {
    render(
      <UndoToast
        message='Retracted "rate-limit"'
        applyAt="2026-05-30T12:00:30Z"
        state={{ status: "undoing" }}
        onUndo={() => {}}
        onExpired={() => {}}
        onDismiss={() => {}}
      />,
    );

    const button = screen.getByTestId("undo-toast-undo");
    expect(button).toHaveTextContent("Undoing…");
    expect(button).toBeDisabled();
  });
});
