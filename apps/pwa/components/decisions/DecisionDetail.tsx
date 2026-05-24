"use client";

import { acceptProposal, rejectProposal } from "@/lib/api/decisions";
import type { Proposal } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { proposalVerb } from "./Decisions";

type DetailState = "idle" | "accepting" | "rejecting" | "error";

/**
 * The focused detail / resolve panel (Stitch screen 5bf54bdf… "Scope Policy
 * Fork"). Shows what the proposal IS — the plain-language verb, its kind chip,
 * the affected vault path (the linked action the merge/create would touch) and
 * the score the scorer assigned — then the two resolve affordances: Accept
 * (apply every linked typed action) and Reject (leave the graph untouched,
 * with an optional reason). After a resolve the container re-reads and the item
 * leaves Pending. A failed call keeps the panel actionable with a calm message.
 *
 * The proposal is addressed by its vault path (`item.id`, a `:path`), which the
 * clients URL-encode whole.
 *
 * DEFERRED vs the Stitch mock (the proposal payload does NOT carry these — see
 * the PR): the "Stay strict / Be liberal" policy-fork radio, the free-text
 * "why you" rationale, the run/context block + tool output, and "see the diff
 * first". Built only the affordances the real API supports.
 */
export default function DecisionDetail({
  item,
  onClose,
  onResolved,
}: {
  item: Proposal;
  onClose: () => void;
  onResolved: () => void;
}) {
  const t = useTranslations("decisions");
  const [state, setState] = useState<DetailState>("idle");
  const [reason, setReason] = useState("");
  const busy = state === "accepting" || state === "rejecting";
  const verb = proposalVerb(item);

  // Escape closes the panel (the backdrop click does too, below). Mirrors the
  // Direct-action / Connectors modal pattern so the native <dialog> handles
  // focus and the listener handles dismiss.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function run(action: "accept" | "reject") {
    if (busy) return;
    setState(action === "accept" ? "accepting" : "rejecting");
    try {
      if (action === "accept") {
        await acceptProposal(item.id);
      } else {
        await rejectProposal(item.id, reason.trim());
      }
      onResolved();
    } catch {
      setState("error");
    }
  }

  return (
    <div className="decisions-detail__overlay">
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: backdrop dismiss; Escape handled above */}
      <div className="decisions-detail__backdrop" onClick={onClose} aria-hidden="true" />
      <dialog className="decisions-detail" aria-label={verb} open>
        <header className="decisions-detail__head">
          <span className="decisions-chip">{item.proposal_kind}</span>
          <button
            type="button"
            className="decisions-detail__close"
            aria-label={t("close")}
            onClick={onClose}
          >
            ✕
          </button>
        </header>

        <h2 className="decisions-detail__title">{verb}</h2>
        <p className="decisions-detail__why">{t("proposalRationale")}</p>

        <dl className="decisions-detail__facts">
          <div className="decisions-detail__fact">
            <dt>{t("affects")}</dt>
            <dd className="decisions-detail__path">{item.action_path}</dd>
          </div>
          {item.score !== null ? (
            <div className="decisions-detail__fact">
              <dt>{t("confidence")}</dt>
              <dd>{Math.round(item.score)}</dd>
            </div>
          ) : null}
        </dl>

        <label className="decisions-detail__reason-label" htmlFor="decision-reason">
          {t("reasonLabel")}
        </label>
        <textarea
          id="decision-reason"
          className="decisions-detail__reason"
          rows={2}
          placeholder={t("reasonPlaceholder")}
          value={reason}
          disabled={busy}
          onChange={(e) => setReason(e.target.value)}
        />

        <div className="decisions-detail__foot">
          {state === "error" && (
            <span className="decisions-row__error" aria-live="polite">
              {t("resolveError")}
            </span>
          )}
          <button
            type="button"
            className="decisions-row__secondary"
            onClick={() => run("reject")}
            disabled={busy}
          >
            {state === "rejecting" ? t("working") : t("reject")}
          </button>
          <button
            type="button"
            className="decisions-row__primary"
            onClick={() => run("accept")}
            disabled={busy}
          >
            {state === "accepting" ? t("working") : t("accept")}
          </button>
        </div>
      </dialog>
    </div>
  );
}
