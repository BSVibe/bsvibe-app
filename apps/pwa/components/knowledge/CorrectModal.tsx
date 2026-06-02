"use client";

/**
 * CorrectModal — the founder-facing "Correct" surface (design §3.3).
 *
 * M3a backend accepts a whitelisted field → new-value mapping under
 * `corrections`. Per design §3.3 the MVP surface edits **body text** only — a
 * single textarea for the replacement content (no markdown editor, no
 * frontmatter JSON, no rich text). The endpoint validates the field whitelist
 * server-side, so additional fields will arrive in a future lift without a
 * wire break.
 *
 * Same undo affordance as Retract (the parent owns the POST + toast) — design
 * §3.3 keeps a 30s undo on correct too, even though correct is a forward-edit
 * (matches the M3a backend's uniform `apply_at` discipline; nothing in the
 * undo wire is action-specific).
 */

import { useTranslations } from "next-intl";
import { type KeyboardEvent, useCallback, useEffect, useId, useRef, useState } from "react";

const MAX_REASON_CHARS = 280;

export interface CorrectModalProps {
  /** The display name of the node being corrected (rendered in the heading). */
  nodeName: string;
  /** Submit handler — parent owns the network call. The replacement text is
   *  the new body content; `reason` is the optional founder-typed free text. */
  onConfirm: (args: { replacement: string; reason: string }) => Promise<void>;
  /** Cancel handler — closes the modal without a POST. */
  onCancel: () => void;
}

export default function CorrectModal({ nodeName, onConfirm, onCancel }: CorrectModalProps) {
  const t = useTranslations("knowledge");
  const [replacement, setReplacement] = useState("");
  const [reason, setReason] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const headingId = useId();

  useEffect(() => {
    dialogRef.current?.focus();
  }, []);

  const canSubmit = replacement.trim().length > 0 && !pending;

  const handleSubmit = useCallback(async () => {
    if (!canSubmit) return;
    setError(null);
    setPending(true);
    try {
      await onConfirm({ replacement: replacement.trim(), reason: reason.trim() });
    } catch {
      setError(t("correctModalError"));
      setPending(false);
    }
  }, [canSubmit, onConfirm, replacement, reason, t]);

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
          {t("correctModalHeading", { name: nodeName })}
        </h2>

        <p className="ontology-modal__lede">{t("correctModalLede")}</p>

        <label className="ontology-modal__field">
          <span className="ontology-modal__field-label">{t("correctModalReplacementLabel")}</span>
          <textarea
            className="ontology-modal__textarea ontology-modal__textarea--large"
            value={replacement}
            onChange={(e) => setReplacement(e.target.value)}
            rows={6}
            placeholder={t("correctModalReplacementPlaceholder")}
            disabled={pending}
            required
          />
        </label>

        <label className="ontology-modal__field">
          <span className="ontology-modal__field-label">{t("correctModalReasonLabel")}</span>
          <input
            type="text"
            className="ontology-modal__input"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            maxLength={MAX_REASON_CHARS}
            placeholder={t("correctModalReasonPlaceholder")}
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
            {t("correctModalCancel")}
          </button>
          <button
            type="button"
            className="ontology-modal__button ontology-modal__button--primary"
            onClick={handleSubmit}
            disabled={!canSubmit}
          >
            {pending ? t("correctModalSubmitting") : t("correctModalConfirm")}
          </button>
        </div>
      </div>
    </div>
  );
}
