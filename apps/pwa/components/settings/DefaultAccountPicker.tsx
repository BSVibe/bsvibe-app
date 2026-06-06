"use client";

import { listAccounts } from "@/lib/api/accounts";
import type { ModelAccount } from "@/lib/api/types";
import { type WorkspaceInfo, getWorkspace, setWorkspaceDefaultAccount } from "@/lib/api/workspace";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

/**
 * Settings → Models → "Default model for this workspace" picker (Lift E2).
 *
 * Renders a dropdown of the workspace's ACTIVE ModelAccounts and the
 * workspace's current `default_account_id`. The dispatch resolver
 * (`backend/dispatch/resolver.py`) consults this column when no
 * `RunRoutingRule` matches a caller; clearing the value with "None" tells
 * the resolver to hard-fail (`NoMatchingRouteError`) instead of silently
 * picking a model — the founder policy `bsvibe-no-implicit-routing`.
 *
 * Reads from `GET /api/v1/workspace` + `GET /api/v1/accounts` on mount;
 * writes via `PATCH /api/v1/workspace { default_account_id }`. A failed
 * read degrades to an inline note; a failed write surfaces inline next to
 * the picker (the value reverts to its server state).
 */
type ListState =
  | { kind: "loading" }
  | { kind: "ready"; workspace: WorkspaceInfo; accounts: ModelAccount[] }
  | { kind: "failed" };

export default function DefaultAccountPicker() {
  const t = useTranslations("settings.models.default");
  const [state, setState] = useState<ListState>({ kind: "loading" });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    Promise.all([getWorkspace(), listAccounts()])
      .then(([workspace, accounts]) => {
        if (!active) return;
        setState({
          kind: "ready",
          workspace,
          accounts: accounts.filter((a) => a.is_active),
        });
      })
      .catch(() => active && setState({ kind: "failed" }));
    return () => {
      active = false;
    };
  }, []);

  async function onChange(value: string) {
    if (state.kind !== "ready") return;
    setSaving(true);
    setError(null);
    try {
      const next = await setWorkspaceDefaultAccount(value === "" ? null : value);
      setState({ kind: "ready", workspace: next, accounts: state.accounts });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  if (state.kind === "loading") {
    return (
      <section className="default-account" aria-label={t("sectionLabel")}>
        <h2 className="section-label">{t("sectionHeading")}</h2>
        <p className="default-account__loading" aria-busy="true">
          {t("loading")}
        </p>
      </section>
    );
  }

  if (state.kind === "failed") {
    return (
      <section className="default-account" aria-label={t("sectionLabel")}>
        <h2 className="section-label">{t("sectionHeading")}</h2>
        <p className="default-account__note" aria-live="polite">
          {t("loadError")}
        </p>
      </section>
    );
  }

  const current = state.workspace.default_account_id ?? "";

  return (
    <section className="default-account" aria-label={t("sectionLabel")}>
      <h2 className="section-label">{t("sectionHeading")}</h2>
      <p className="default-account__lede">{t("lede")}</p>
      <label className="default-account__field">
        <span className="default-account__label-text">{t("pickerLabel")}</span>
        <select
          className="default-account__select"
          value={current}
          onChange={(e) => void onChange(e.target.value)}
          disabled={saving}
          aria-label={t("pickerLabel")}
        >
          <option value="">{t("none")}</option>
          {state.accounts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.label} ({a.litellm_model})
            </option>
          ))}
        </select>
      </label>
      {state.accounts.length === 0 ? (
        <p className="default-account__hint">{t("noActiveAccounts")}</p>
      ) : null}
      {error ? (
        <p className="default-account__error" aria-live="polite">
          {error}
        </p>
      ) : null}
    </section>
  );
}
