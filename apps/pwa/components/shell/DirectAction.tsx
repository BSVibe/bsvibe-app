"use client";

import { useEffect } from "react";
import { PlusIcon } from "./icons";

/** Floating "+ Direct" trigger — the global compose affordance (UX §1.1). */
export function DirectFab({ onClick }: { onClick: () => void }) {
  return (
    <button type="button" className="direct-fab" onClick={onClick}>
      <PlusIcon />
      <span>Direct</span>
    </button>
  );
}

/**
 * Direct compose overlay — a STUB. Direct is a global action, not a page
 * (UX §4); the full fluid-input compose lands later. This proves the
 * affordance: ⌘K / FAB open it, Escape or the backdrop closes it.
 */
export function DirectOverlay({ open, onClose }: { open: boolean; onClose: () => void }) {
  useEffect(() => {
    if (!open) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="direct-overlay">
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: backdrop dismiss; Escape handled above */}
      <div className="direct-overlay__backdrop" onClick={onClose} aria-hidden="true" />
      <dialog className="direct-overlay__panel" aria-label="Direct" open>
        <p className="direct-overlay__hint">
          Tell BSVibe what to do — ask a question or start work.
        </p>
        <textarea
          className="direct-overlay__input"
          placeholder="e.g. “draft the launch post for bsvibe-site”"
          rows={3}
          disabled
        />
        <p className="direct-overlay__soon">Direct compose is coming soon.</p>
      </dialog>
    </div>
  );
}
