"use client";

/**
 * InspectorActions — the founder-facing write surface on the Inside view
 * inspector panel (Lift M3b PWA half of the M3a backend). Renders the
 * Correct + Retract buttons (design §3.1), drives the modals, and owns the
 * 30-second undo toast lifecycle.
 *
 * The component is the integration point between the read-only inspector
 * panel and the M3a write endpoints (lib/api/knowledge.ts:
 * `retractNode`/`correctNode`/`undoCorrection`). It keeps its own state
 * machine — `idle → modal → toast(countdown) → toast(terminal)` — so the
 * panel host (KnowledgeGraphView) stays read-shaped: it just hands us the
 * node ref/name and an `onAfterApply` hook for cosmetic updates (fading the
 * retracted node, badging the corrected one).
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
import { correctNode, retractNode, undoCorrection } from "@/lib/api/knowledge";
import type { RetractionSignal } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useCallback, useState } from "react";
import CorrectModal from "./CorrectModal";
import RetractModal from "./RetractModal";
import UndoToast, { type ToastState } from "./UndoToast";

export type ActionKind = "retract" | "correct";

export interface InspectorActionsProps {
  /** The vault path / concept id the M3a `nodes/{node_ref}/...` endpoints
   *  take. Backend resolves it; we just thread it through unchanged. */
  nodeRef: string;
  /** Display name for modal headings + toast confirmation. */
  nodeName: string;
  /** Fired once a retract/correct ENTERS the toast countdown — the parent
   *  uses it to mark the node visually in the current session (retract:
   *  fade, correct: edited badge). NOT fired on cancel or modal error. */
  onApplied?: (action: ActionKind) => void;
  /** Fired once the toast hits a terminal state where the parent may want
   *  to refetch the graph (per design Q6a: hide retracted on next load).
   *  `undone` cases skip the refetch — the node is staying. */
  onWindowClosed?: (action: ActionKind, finalState: "expired" | "applied") => void;
}

type Phase =
  | { kind: "idle" }
  | { kind: "modal-retract" }
  | { kind: "modal-correct" }
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
  const startCorrect = useCallback(() => setPhase({ kind: "modal-correct" }), []);
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

  const handleCorrectConfirm = useCallback(
    async ({ replacement, reason }: { replacement: string; reason: string }) => {
      const body: { reason?: string; corrections: Record<string, string> } = {
        corrections: { body: replacement },
      };
      if (reason.length > 0) body.reason = reason;
      const res = await correctNode(nodeRef, body);
      enterToastCountdown("correct", res.signal);
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
          onClick={startCorrect}
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

      {phase.kind === "modal-correct" ? (
        <CorrectModal nodeName={nodeName} onConfirm={handleCorrectConfirm} onCancel={closeModal} />
      ) : null}

      {phase.kind === "toast" ? (
        <UndoToast
          message={
            phase.action === "retract"
              ? t("undoToastRetractedMessage", { name: nodeName })
              : t("undoToastCorrectedMessage", { name: nodeName })
          }
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
