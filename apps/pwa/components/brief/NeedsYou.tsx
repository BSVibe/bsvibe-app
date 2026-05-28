"use client";

import { approveSafeModeItem, denySafeModeItem } from "@/lib/api/safemode";
import type { NeedsYouItem } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useState } from "react";

/**
 * The "Needs you" strip — the one thing that genuinely requires the founder
 * (UX §3.2 principle 1), pinned to the top. Empty → a calm quiet state.
 *
 * Safe-Mode held deliveries are the founder's first real "Decide" action: each
 * carries Approve / Deny affordances (UX moment: Decide). Approve dispatches
 * the held delivery out; Deny dismisses it. Canonicalization proposals have no
 * PWA resolve endpoint yet, so they render read-only (no `resolve`).
 *
 * `onResolved` is invoked after a successful approve/deny so the container can
 * re-read the Brief and drop the resolved item.
 */
export default function NeedsYou({
  items,
  onResolved,
}: {
  items: NeedsYouItem[];
  onResolved?: () => void;
}) {
  const t = useTranslations("brief");
  if (items.length === 0) {
    return (
      <section className="needs-you needs-you--empty" aria-label={t("needsYou")}>
        <p className="needs-you__clear">{t("nothingNeedsYou")}</p>
      </section>
    );
  }

  return (
    <section className="needs-you" aria-label={t("needsYou")}>
      <header className="needs-you__head">
        <span className="needs-you__title">
          {t("needsYou")} <span aria-hidden="true">👋</span>
        </span>
        <span className="needs-you__count">{items.length}</span>
      </header>
      <ul className="needs-you__list">
        {items.map((item) => (
          <NeedsYouRow key={item.id} item={item} onResolved={onResolved} />
        ))}
      </ul>
    </section>
  );
}

type ResolveState = "idle" | "resolving" | "approved" | "denied" | "error";

/** One "Needs you" row. Read-only unless it carries a `resolve` (Safe-Mode),
 *  in which case it shows Approve / Deny with in-flight, resolved, and calm
 *  inline error states. A failed action does NOT crash the strip. */
function NeedsYouRow({ item, onResolved }: { item: NeedsYouItem; onResolved?: () => void }) {
  const [state, setState] = useState<ResolveState>("idle");
  const t = useTranslations("brief");

  async function run(action: "approve" | "deny") {
    if (!item.resolve || state === "resolving") return;
    setState("resolving");
    try {
      if (action === "approve") {
        await approveSafeModeItem(item.resolve.itemId);
        setState("approved");
      } else {
        await denySafeModeItem(item.resolve.itemId);
        setState("denied");
      }
      // Let the container re-read the Brief so the resolved item drops out.
      onResolved?.();
    } catch {
      // Any failure (ApiError or network) shows the same calm inline message;
      // the row stays actionable and the strip stays up — no re-read fires.
      setState("error");
    }
  }

  const resolved = state === "approved" || state === "denied";

  return (
    <li className={`needs-you__row needs-you__row--${state}`}>
      <span className="needs-you__product">{item.productSlug}</span>
      <span className="needs-you__sep" aria-hidden="true">
        ·
      </span>
      <span className="needs-you__q">{item.question}</span>

      {item.resolve && !resolved ? (
        <span className="needs-you__actions">
          {state === "error" && (
            <span className="needs-you__error" aria-live="polite">
              {t("resolveError")}
            </span>
          )}
          <button
            type="button"
            className="needs-you__deny"
            onClick={() => run("deny")}
            disabled={state === "resolving"}
          >
            {t("deny")}
          </button>
          <button
            type="button"
            className="needs-you__approve"
            onClick={() => run("approve")}
            disabled={state === "resolving"}
          >
            {state === "resolving" ? t("working") : t("approve")}
          </button>
        </span>
      ) : null}

      {resolved && (
        <span className="needs-you__resolved" aria-live="polite">
          {state === "approved" ? t("approved") : t("dismissed")}
        </span>
      )}

      {!item.resolve && (
        <span className="needs-you__chev" aria-hidden="true">
          ›
        </span>
      )}
    </li>
  );
}
