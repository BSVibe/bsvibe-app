"use client";

import type { ModelAccount } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useState } from "react";

type RowState = "idle" | "toggling" | "confirming" | "revoking" | "error";

/**
 * One registered model account. Shows the label, provider + litellm model, the
 * active state, and a masked "key on file" hint (never the credential). Two
 * actions:
 *
 *  - Activate / Deactivate — a PATCH flipping `is_active`. The agent loop's
 *    model-account resolution pauses a run when there is zero (or ambiguous)
 *    active accounts, so this toggle is the founder's switch for "use this one".
 *  - Revoke — confirm-gated DELETE (hard delete). The first "Revoke" click
 *    reveals a "Confirm revoke" (+ "Cancel") so it's never a single stray tap.
 *
 * On success `onChanged` re-reads the list. A failed action shows a calm inline
 * note and keeps the row actionable — it never crashes the surface.
 *
 * `setActive` / `revoke` are injected (default to the real client at the call
 * site) so the surface is unit-testable against a mocked fetch.
 */
export default function ModelAccountRow({
  account,
  onChanged,
  setActive,
  revoke,
}: {
  account: ModelAccount;
  onChanged: () => void;
  setActive: (id: string, isActive: boolean) => Promise<ModelAccount>;
  revoke: (id: string) => Promise<void>;
}) {
  const [state, setState] = useState<RowState>("idle");
  const t = useTranslations("settings.models");

  async function toggle() {
    if (state === "toggling" || state === "revoking") return;
    setState("toggling");
    try {
      await setActive(account.id, !account.is_active);
      // Unlike revoke, a toggle keeps this account in the list (same `key`), so
      // this component instance persists. Reset to idle so the re-rendered row
      // (now reflecting the new `is_active` via props) is actionable again — the
      // in-flight `toggling` guard above already prevented a double-fire.
      setState("idle");
      onChanged();
    } catch {
      setState("error");
    }
  }

  async function confirmRevoke() {
    if (state === "revoking") return;
    setState("revoking");
    try {
      await revoke(account.id);
      onChanged();
    } catch {
      setState("error");
    }
  }

  const busy = state === "toggling" || state === "revoking";

  return (
    <li className="account-row">
      <div className="account-row__main">
        <span className="account-row__label">{account.label}</span>
        <span className="account-row__model">
          {account.provider} · {account.litellm_model}
        </span>
        {account.has_api_key ? (
          <span className="account-row__key" title={t("keyOnFileTitle")}>
            {t("keyOnFile")}
          </span>
        ) : (
          <span className="account-row__key account-row__key--missing" title={t("noKeyTitle")}>
            {t("noKey")}
          </span>
        )}
        <span
          className={`account-row__state account-row__state--${
            account.is_active ? "active" : "inactive"
          }`}
        >
          {account.is_active ? t("active") : t("inactive")}
        </span>
      </div>

      <div className="account-row__actions">
        {state === "error" && (
          <span className="account-row__error" aria-live="polite">
            {t("rowError")}
          </span>
        )}

        {state === "confirming" || state === "revoking" ? (
          <>
            <button
              type="button"
              className="account-row__danger"
              onClick={confirmRevoke}
              disabled={state === "revoking"}
            >
              {state === "revoking" ? t("revoking") : t("confirmRevoke")}
            </button>
            <button
              type="button"
              className="account-row__cancel"
              onClick={() => setState("idle")}
              disabled={state === "revoking"}
            >
              {t("cancel")}
            </button>
          </>
        ) : (
          <>
            <button type="button" className="account-row__toggle" onClick={toggle} disabled={busy}>
              {state === "toggling"
                ? account.is_active
                  ? t("deactivating")
                  : t("activating")
                : account.is_active
                  ? t("deactivate")
                  : t("activate")}
            </button>
            <button
              type="button"
              className="account-row__revoke"
              onClick={() => setState("confirming")}
              disabled={busy}
            >
              {t("revoke")}
            </button>
          </>
        )}
      </div>
    </li>
  );
}
