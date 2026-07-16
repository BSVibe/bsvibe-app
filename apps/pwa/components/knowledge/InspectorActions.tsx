"use client";

/**
 * InspectorActions — the founder-facing write surface on the Inside view
 * inspector panel. Renders the Retract action (design §3.1), drives its modal,
 * and owns the 30-second undo toast lifecycle.
 *
 * The Correct action is rendered as a DISABLED "coming soon" affordance: the
 * in-place field-rewrite editor was never built (the backend `correct` path
 * refuses honestly), so we must not offer a control that confirms a correction
 * — and writes a false "Corrected X" + undo toast — for an operation that
 * mutates nothing. Retract is the working mutation.
 *
 * The component is the integration point between the read-only inspector
 * panel and the write endpoints (lib/api/knowledge.ts:
 * `retractNode`/`undoCorrection`). It keeps its own state machine —
 * `idle → modal → toast(countdown) → toast(terminal)` — so the panel host
 * (KnowledgeGraphView) stays read-shaped: it just hands us the node ref/name
 * and an `onApplied` hook for cosmetic updates (fading the retracted node).
 *
 * Design invariants preserved:
 *  - 30s undo window comes from `signal.apply_at` (server-stamped wall-clock).
 *  - Undo is idempotent + server-authoritative: the toast posts to
 *    `/corrections/{id}/undo` and surfaces the terminal status; a window-
 *    expiry tick races the server, both paths converge.
 *  - The toast persists until the founder dismisses it (terminal state) OR
 *    a new action displaces it (the parent unmounts us on next selection).
 */

import { ApiError } from "@/lib/api/client";
import { retractNode, undoCorrection } from "@/lib/api/knowledge";
import type { RetractionSignal } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useCallback, useState } from "react";
import RetractModal from "./RetractModal";
import UndoToast, { type ToastState } from "./UndoToast";

export type ActionKind = "retract" | "correct";

export interface InspectorActionsProps {
  /** The vault path / concept id the `nodes/{node_ref}/...` endpoints
   *  take. Backend resolves it; we just thread it through unchanged. */
  nodeRef: string;
  /** Display name for modal headings + toast confirmation. */
  nodeName: string;
  /** Fired once a retract ENTERS the toast countdown — the parent uses it to
   *  mark the node visually in the current session (retract: fade). NOT fired
   *  on cancel or modal error. */
  onApplied?: (action: ActionKind) => void;
  /** Fired once the toast hits a terminal state where the parent may want
   *  to refetch the graph (per design Q6a: hide retracted on next load).
   *  `undone` cases skip the refetch — the node is staying. */
  onWindowClosed?: (action: ActionKind, finalState: "expired" | "applied") => void;
}

type Phase =
  | { kind: "idle" }
  | { kind: "modal-retract" }
  | {
      kind: "toast";
      action: ActionKind;
      correctionId: string;
      applyAt: string;
      state: ToastState;
    };

export default function InspectorActions({
  nodeRef,
  nodeName,
  onApplied,
  onWindowClosed,
}: InspectorActionsProps) {
  const t = useTranslations("knowledge");
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });

  const startRetract = useCallback(() => setPhase({ kind: "modal-retract" }), []);
  const closeModal = useCallback(() => setPhase({ kind: "idle" }), []);

  const enterToastCountdown = useCallback(
    (action: ActionKind, signal: RetractionSignal) => {
      onApplied?.(action);
      setPhase({
        kind: "toast",
        action,
        correctionId: signal.id,
        applyAt: signal.apply_at,
        state: { status: "countdown" },
      });
    },
    [onApplied],
  );

  const handleRetractConfirm = useCallback(
    async (reason: string) => {
      const body = reason.length > 0 ? { reason } : {};
      const res = await retractNode(nodeRef, body);
      enterToastCountdown("retract", res.signal);
    },
    [nodeRef, enterToastCountdown],
  );

  const handleUndo = useCallback(async () => {
    if (phase.kind !== "toast") return;
    const { correctionId, action } = phase;
    setPhase((prev) => (prev.kind === "toast" ? { ...prev, state: { status: "undoing" } } : prev));
    try {
      const res = await undoCorrection(correctionId);
      // The backend returns the terminal status: undone / expired / already_*.
      // We render the calm friendly version of each; expired triggers the same
      // "window closed" hook as the auto-expiry timer.
      if (res.status === "undone" || res.status === "already_undone") {
        setPhase((prev) =>
          prev.kind === "toast" ? { ...prev, state: { status: "undone" } } : prev,
        );
      } else if (res.status === "expired" || res.status === "already_applied") {
        setPhase((prev) =>
          prev.kind === "toast" ? { ...prev, state: { status: "expired" } } : prev,
        );
        onWindowClosed?.(action, res.status === "expired" ? "expired" : "applied");
      }
    } catch (err) {
      // 404 = correction not found server-side (raced a sweep). Surface as
      // expired so the founder sees a calm terminal state.
      if (err instanceof ApiError && err.status === 404) {
        setPhase((prev) =>
          prev.kind === "toast" ? { ...prev, state: { status: "expired" } } : prev,
        );
        onWindowClosed?.(action, "expired");
      } else {
        setPhase((prev) =>
          prev.kind === "toast" ? { ...prev, state: { status: "error" } } : prev,
        );
      }
    }
  }, [phase, onWindowClosed]);

  const handleToastExpired = useCallback(() => {
    setPhase((prev) => {
      if (prev.kind !== "toast" || prev.state.status !== "countdown") return prev;
      onWindowClosed?.(prev.action, "applied");
      return { ...prev, state: { status: "expired" } };
    });
  }, [onWindowClosed]);

  const dismissToast = useCallback(() => setPhase({ kind: "idle" }), []);

  return (
    <>
      <div className="ontology-actions">
        <button
          type="button"
          className="ontology-actions__button ontology-actions__button--secondary"
          disabled
          title={t("inspectorActionCorrectUnavailable")}
          data-testid="inspector-action-correct"
        >
          {t("inspectorActionCorrect")}
        </button>
        <button
          type="button"
          className="ontology-actions__button ontology-actions__button--danger"
          onClick={startRetract}
          data-testid="inspector-action-retract"
        >
          {t("inspectorActionRetract")}
        </button>
      </div>

      {phase.kind === "modal-retract" ? (
        <RetractModal nodeName={nodeName} onConfirm={handleRetractConfirm} onCancel={closeModal} />
      ) : null}

      {phase.kind === "toast" ? (
        <UndoToast
          message={t("undoToastRetractedMessage", { name: nodeName })}
          applyAt={phase.applyAt}
          state={phase.state}
          onUndo={() => void handleUndo()}
          onExpired={handleToastExpired}
          onDismiss={dismissToast}
        />
      ) : null}
    </>
  );
}
