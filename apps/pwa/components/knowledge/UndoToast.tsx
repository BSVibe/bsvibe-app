"use client";

/**
 * UndoToast — the calm 30-second "Undo" affordance the founder sees after a
 * retract / correct lands (design §3.2 / §3.4). Renders an "Undone" status
 * on success, "Undo window expired" on the 30s timeout, or the live
 * countdown otherwise. The toast lives in the parent until it terminates
 * (founder pressed Undo, or the window closed, or the founder dismissed it).
 *
 * Countdown timing: a plain `setInterval` ticks every 1000ms re-deriving the
 * remaining seconds from `apply_at - now()` (NOT a naive `seconds--`) so a
 * paused tab / suspended event loop snaps back to the wall-clock remainder
 * on resume. When the remainder hits 0 the toast fires `onExpired` exactly
 * once — the parent uses it to (a) optionally trigger a backend undo POST
 * that returns `expired` to render the terminal state, or (b) just fade out.
 *
 * `applyAt` is the server-stamped wall-clock deadline (ISO-8601 from
 * `RetractionSignal.apply_at`); the component never trusts the client clock
 * for issued_at, so a stale modal that sat open for 20s before submit still
 * gets the right wall-clock remainder.
 */

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";

export type ToastState =
  | { status: "countdown" }
  | { status: "undoing" }
  | { status: "undone" }
  | { status: "expired" }
  | { status: "error" };

export interface UndoToastProps {
  /** Human-readable confirmation line, e.g. "Retracted 'rate-limit'". */
  message: string;
  /** Server-stamped wall-clock deadline (ISO-8601). The countdown derives
   *  the remainder from `apply_at - now()` every tick. */
  applyAt: string;
  /** Terminal-state surface — the parent decides whether to show the toast
   *  at all. Drives the rendered text + the Undo button visibility. */
  state: ToastState;
  /** Fires when the founder clicks Undo. Parent owns the POST + state. */
  onUndo: () => void;
  /** Fires once when the countdown crosses zero (so the parent can fade /
   *  re-fetch the graph / etc.). */
  onExpired: () => void;
  /** Fires when the founder dismisses the toast (× button). */
  onDismiss: () => void;
}

/** Returns the remaining whole seconds between now and `applyAt` (clamped
 *  ≥ 0). Exported for testability; the component recomputes per tick. */
export function remainingSeconds(applyAt: string, now: number = Date.now()): number {
  const deadlineMs = Date.parse(applyAt);
  if (!Number.isFinite(deadlineMs)) return 0;
  return Math.max(0, Math.ceil((deadlineMs - now) / 1000));
}

export default function UndoToast({
  message,
  applyAt,
  state,
  onUndo,
  onExpired,
  onDismiss,
}: UndoToastProps) {
  const t = useTranslations("knowledge");
  const [remaining, setRemaining] = useState(() => remainingSeconds(applyAt));

  // Drive the countdown only while we are in the countdown state. Each tick
  // re-derives from wall-clock so a paused tab snaps back to the true
  // remainder. When the remainder hits 0 we fire `onExpired` once.
  useEffect(() => {
    if (state.status !== "countdown") return;
    // Sync once on entry to the countdown state — the initial state might
    // be stale by hundreds of ms from when applyAt was server-stamped.
    setRemaining(remainingSeconds(applyAt));
    const interval = window.setInterval(() => {
      const next = remainingSeconds(applyAt);
      setRemaining(next);
      if (next <= 0) {
        window.clearInterval(interval);
        onExpired();
      }
    }, 1000);
    return () => {
      window.clearInterval(interval);
    };
  }, [state.status, applyAt, onExpired]);

  const handleUndo = useCallback(() => {
    if (state.status === "countdown") onUndo();
  }, [state.status, onUndo]);

  const showUndoButton = state.status === "countdown" || state.status === "undoing";
  const dismissed =
    state.status === "undone" || state.status === "expired" || state.status === "error";

  let statusLine: string = message;
  if (state.status === "undone") statusLine = t("undoToastRestored");
  else if (state.status === "expired") statusLine = t("undoToastExpired");
  else if (state.status === "error") statusLine = t("undoToastError");

  return (
    <output
      className={`ontology-toast ontology-toast--${state.status}`}
      aria-live="polite"
      data-testid="undo-toast"
    >
      <div className="ontology-toast__body">
        <p className="ontology-toast__message">{statusLine}</p>
      </div>
      <div className="ontology-toast__actions">
        {showUndoButton ? (
          <button
            type="button"
            className="ontology-toast__undo"
            onClick={handleUndo}
            disabled={state.status === "undoing"}
            data-testid="undo-toast-undo"
          >
            {state.status === "undoing"
              ? t("undoToastUndoing")
              : t("undoToastUndoWithSeconds", { seconds: remaining })}
          </button>
        ) : null}
        {dismissed ? (
          <button
            type="button"
            className="ontology-toast__dismiss"
            onClick={onDismiss}
            aria-label={t("undoToastDismissLabel")}
          >
            ×
          </button>
        ) : null}
      </div>
    </output>
  );
}
