"use client";

import { ApiError } from "@/lib/api/client";
import { submitMessage } from "@/lib/api/messages";
import { listProducts } from "@/lib/api/products";
import { useTranslations } from "next-intl";
import { usePathname } from "next/navigation";
import { type FormEvent, useEffect, useState } from "react";
import { PlusIcon } from "./icons";

/** Fired on a successful Direct submission so the Brief can optimistically
 *  reflect that a new run is in flight (re-fetch its lanes). */
export const DIRECT_SUBMITTED_EVENT = "bsvibe:direct-submitted";

/** Floating "+ Direct" trigger — the global compose affordance (UX §1.1). */
export function DirectFab({ onClick }: { onClick: () => void }) {
  const t = useTranslations("direct");
  return (
    <button type="button" className="direct-fab" onClick={onClick}>
      <PlusIcon />
      <span>{t("label")}</span>
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
/** Parse ``/products/<slug>`` (anywhere in the path — locale isn't in the
 *  URL, but defensive regex covers any future prefixing). Returns the slug
 *  segment or ``null`` when the founder isn't on a product page.
 *
 *  Without this, the Direct dialog on a product page submitted with no
 *  product_id and the backend's smart-default sent the run to the
 *  workspace's *earliest* product (L-P1 fallback) — surfaced in the W2
 *  dogfood when a message on /products/w2-dogfood landed on e2e-hello.
 */
function _currentProductSlug(pathname: string | null): string | null {
  if (!pathname) return null;
  const match = pathname.match(/^\/products\/([^/?#]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

export function DirectOverlay({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [text, setText] = useState("");
  const [state, setState] = useState<SubmitState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [productId, setProductId] = useState<string | null>(null);
  const t = useTranslations("direct");
  const pathname = usePathname();
  const currentSlug = _currentProductSlug(pathname);

  // Resolve the current product slug → product_id when the overlay opens
  // on a product page. Best-effort: a failed list call falls back to
  // submitting without product_id (the backend's workspace-default
  // fallback still produces a valid run, just bound to the default
  // product). Cached implicitly by the browser per request lifecycle.
  useEffect(() => {
    if (!open || !currentSlug) {
      setProductId(null);
      return;
    }
    let cancelled = false;
    listProducts()
      .then((products) => {
        if (cancelled) return;
        const match = products.find((p) => p.slug === currentSlug);
        setProductId(match?.id ?? null);
      })
      .catch(() => {
        if (!cancelled) setProductId(null);
      });
    return () => {
      cancelled = true;
    };
  }, [open, currentSlug]);

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
      await submitMessage({
        text: trimmed,
        ...(productId ? { product_id: productId } : {}),
      });
      setState("success");
      // Optimistically nudge the Brief to re-read its lanes.
      window.dispatchEvent(new CustomEvent(DIRECT_SUBMITTED_EVENT));
    } catch (err) {
      setState("error");
      setError(err instanceof ApiError ? t("errorSend") : t("errorNetwork"));
    }
  }

  return (
    <div className="direct-overlay">
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: backdrop dismiss; Escape handled above */}
      <div className="direct-overlay__backdrop" onClick={onClose} aria-hidden="true" />
      <dialog className="direct-overlay__panel" aria-label={t("label")} open>
        <form onSubmit={onSubmit}>
          <p className="direct-overlay__hint">{t("hint")}</p>
          <textarea
            className="direct-overlay__input"
            placeholder={t("placeholder")}
            rows={3}
            value={text}
            onChange={(e) => setText(e.target.value)}
            disabled={state === "submitting" || state === "success"}
            // biome-ignore lint/a11y/noAutofocus: focus the one field on open
            autoFocus
          />
          <div className="direct-overlay__foot">
            <span className="direct-overlay__status" aria-live="polite">
              {state === "submitting" && t("sending")}
              {state === "success" && t("sent")}
              {state === "error" && <span className="direct-overlay__error">{error}</span>}
            </span>
            <button type="submit" className="direct-overlay__submit" disabled={!canSubmit}>
              {state === "submitting" ? t("sending") : t("label")}
            </button>
          </div>
        </form>
      </dialog>
    </div>
  );
}
