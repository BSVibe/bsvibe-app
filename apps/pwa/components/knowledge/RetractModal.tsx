"use client";

/**
 * RetractModal — the pre-flight confirmation a founder sees before a retract
 * lands. Matches the design's State A ASCII (§3.2): node name in the heading,
 * a calm one-sentence consequence, an optional `reason` field (design Q2
 * locks low-friction optional), Cancel + Retract buttons.
 *
 * Cascade-dependents (the "This was used in: …" block in the ASCII) is wired
 * structurally — the modal accepts an optional `dependents` list and renders
 * it as a calm bullet list when present — but the M3a backend does NOT yet
 * compute dependents at intake (the field is server-stamped in §2.1 but
 * deferred to the follow-up lift). So the modal hides the block when empty,
 * staying forward-compatible.
 *
 * The modal owns ONLY its UI state (the typed reason text + the submit
 * pending flag). The actual POST + toast wiring lives in the parent
 * (KnowledgeGraphView) so retract and correct share one undo affordance.
 */

import { useTranslations } from "next-intl";
import {
  type KeyboardEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";

const MAX_REASON_CHARS = 280;

export interface RetractModalProps {
  /** The display name of the node being retracted (rendered in the heading). */
  nodeName: string;
  /** Optional cascade-dependents surfaced pre-flight (calm bullet list). */
  dependents?: ReactNode;
  /** Submit handler — resolves to the toast-eligible state. The parent owns
   *  the network call so retract + correct flows share one undo affordance. */
  onConfirm: (reason: string) => Promise<void>;
  /** Cancel handler — closes the modal without a POST. */
  onCancel: () => void;
}

export default function RetractModal({
  nodeName,
  dependents,
  onConfirm,
  onCancel,
}: RetractModalProps) {
  const t = useTranslations("knowledge");
  const [reason, setReason] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const headingId = useId();

  // Autofocus the heading on mount so the modal grabs focus (calm, keyboard-
  // navigable; the textarea is intentionally NOT auto-focused — the design
  // makes `reason` optional, and pulling focus into a textbox would force the
  // founder to escape it before Tab-cycling to the buttons).
  useEffect(() => {
    dialogRef.current?.focus();
  }, []);

  const handleSubmit = useCallback(async () => {
    if (pending) return;
    setError(null);
    setPending(true);
    try {
      await onConfirm(reason.trim());
    } catch {
      // Parent surfaces a toast on success; on failure we show inline so the
      // modal stays open and the founder can retry without retyping `reason`.
      setError(t("retractModalError"));
      setPending(false);
    }
  }, [pending, onConfirm, reason, t]);

  const handleKeyDown = (e: KeyboardEvent) => {
    if (e.key === "Escape") onCancel();
  };

  return (
    <div className="ontology-modal__backdrop" onKeyDown={handleKeyDown}>
      <button
        type="button"
        aria-label={t("modalBackdropDismiss")}
        className="ontology-modal__backdrop-button"
        onClick={onCancel}
        data-testid="modal-backdrop"
      />
      <div
        ref={dialogRef}
        tabIndex={-1}
        // biome-ignore lint/a11y/useSemanticElements: <dialog> tab-traps differently across
        // browsers and our calm modal CSS does positioning. role+aria-modal preserves AT.
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        className="ontology-modal"
      >
        <h2 id={headingId} className="ontology-modal__heading">
          {t("retractModalHeading", { name: nodeName })}
        </h2>

        {dependents ? (
          <div className="ontology-modal__dependents">
            <h3 className="ontology-modal__subheading">{t("retractModalDependentsLabel")}</h3>
            {dependents}
          </div>
        ) : null}

        <p className="ontology-modal__lede">{t("retractModalLede")}</p>

        <label className="ontology-modal__field">
          <span className="ontology-modal__field-label">{t("retractModalReasonLabel")}</span>
          <textarea
            className="ontology-modal__textarea"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            maxLength={MAX_REASON_CHARS}
            rows={2}
            placeholder={t("retractModalReasonPlaceholder")}
            disabled={pending}
          />
        </label>

        {error ? (
          <p className="ontology-modal__error" aria-live="polite">
            {error}
          </p>
        ) : null}

        <div className="ontology-modal__actions">
          <button
            type="button"
            className="ontology-modal__button ontology-modal__button--secondary"
            onClick={onCancel}
            disabled={pending}
          >
            {t("retractModalCancel")}
          </button>
          <button
            type="button"
            className="ontology-modal__button ontology-modal__button--danger"
            onClick={handleSubmit}
            disabled={pending}
          >
            {pending ? t("retractModalSubmitting") : t("retractModalConfirm")}
          </button>
        </div>
      </div>
    </div>
  );
}
