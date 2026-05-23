"use client";

import { ApiError } from "@/lib/api/client";
import { submitMessage } from "@/lib/api/messages";
import { type FormEvent, useEffect, useState } from "react";
import { PlusIcon } from "./icons";

/** Fired on a successful Direct submission so the Brief can optimistically
 *  reflect that a new run is in flight (re-fetch its lanes). */
export const DIRECT_SUBMITTED_EVENT = "bsvibe:direct-submitted";

/** Floating "+ Direct" trigger — the global compose affordance (UX §1.1). */
export function DirectFab({ onClick }: { onClick: () => void }) {
  return (
    <button type="button" className="direct-fab" onClick={onClick}>
      <PlusIcon />
      <span>Direct</span>
    </button>
  );
}

type SubmitState = "idle" | "submitting" | "success" | "error";

/**
 * Direct compose overlay — the global compose action (UX §4). A textarea →
 * `POST /api/v1/messages`; the agent workers drive it the rest of the way.
 * ⌘K / FAB open it, Escape or the backdrop closes it. On success it shows a
 * brief "sent — working on it", emits {@link DIRECT_SUBMITTED_EVENT} so the
 * Brief reflects the new run optimistically, then auto-closes.
 */
export function DirectOverlay({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [text, setText] = useState("");
  const [state, setState] = useState<SubmitState>("idle");
  const [error, setError] = useState<string | null>(null);

  // Reset the form whenever the overlay (re)opens.
  useEffect(() => {
    if (open) {
      setText("");
      setState("idle");
      setError(null);
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // Auto-close shortly after a successful send.
  useEffect(() => {
    if (state !== "success") return;
    const timer = window.setTimeout(onClose, 1100);
    return () => window.clearTimeout(timer);
  }, [state, onClose]);

  if (!open) return null;

  const trimmed = text.trim();
  const canSubmit = trimmed.length > 0 && state !== "submitting" && state !== "success";

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) return;
    setState("submitting");
    setError(null);
    try {
      await submitMessage({ text: trimmed });
      setState("success");
      // Optimistically nudge the Brief to re-read its lanes.
      window.dispatchEvent(new CustomEvent(DIRECT_SUBMITTED_EVENT));
    } catch (err) {
      setState("error");
      setError(
        err instanceof ApiError
          ? "Couldn’t send that — please try again."
          : "Network hiccup — please try again.",
      );
    }
  }

  return (
    <div className="direct-overlay">
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: backdrop dismiss; Escape handled above */}
      <div className="direct-overlay__backdrop" onClick={onClose} aria-hidden="true" />
      <dialog className="direct-overlay__panel" aria-label="Direct" open>
        <form onSubmit={onSubmit}>
          <p className="direct-overlay__hint">
            Tell BSVibe what to do — ask a question or start work.
          </p>
          <textarea
            className="direct-overlay__input"
            placeholder="e.g. “draft the launch post for bsvibe-site”"
            rows={3}
            value={text}
            onChange={(e) => setText(e.target.value)}
            disabled={state === "submitting" || state === "success"}
            // biome-ignore lint/a11y/noAutofocus: focus the one field on open
            autoFocus
          />
          <div className="direct-overlay__foot">
            <span className="direct-overlay__status" aria-live="polite">
              {state === "submitting" && "Sending…"}
              {state === "success" && "Sent — working on it."}
              {state === "error" && <span className="direct-overlay__error">{error}</span>}
            </span>
            <button type="submit" className="direct-overlay__submit" disabled={!canSubmit}>
              {state === "submitting" ? "Sending…" : "Direct"}
            </button>
          </div>
        </form>
      </dialog>
    </div>
  );
}
