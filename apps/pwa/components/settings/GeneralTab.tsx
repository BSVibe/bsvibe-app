"use client";

import { ApiError } from "@/lib/api/client";
import {
  getWorkspace,
  renameWorkspace,
  setWorkspaceLanguage,
  setWorkspaceSafeMode,
} from "@/lib/api/workspace";
import { useSession } from "@/lib/auth/session";
import { type Locale, resolveLocale } from "@/lib/i18n/config";
import { setLocaleCookie } from "@/lib/i18n/locale";
import {
  DATE_FORMAT_OPTIONS,
  LANGUAGE_OPTIONS,
  TIMEZONE_OPTIONS,
} from "@/lib/preferences/preferences";
import { usePreferences } from "@/lib/preferences/usePreferences";
import type { ThemePreference } from "@/lib/theme/theme";
import { useThemePreference } from "@/lib/theme/useTheme";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useState } from "react";

/**
 * Settings → General. Workspace basics + appearance.
 *
 *  - Theme (the headline): a Light / System / Dark segmented control wired to
 *    the theme controller. Choosing one persists `bsvibe.theme` and applies
 *    `data-theme` to <html> immediately. This is the must-work piece.
 *  - Language: writes the `bsvibe.locale` cookie and refreshes the route so the
 *    new message catalog applies live (real i18n, via next-intl). Also kept in
 *    the local preference blob so the select reflects the stored choice.
 *  - Time zone / Date format: LOCAL-only preferences (no backend yet — server
 *    sync is a follow-up).
 *  - Workspace name / Workspace ID: DISPLAY-only. We surface what the client
 *    already knows (the session's personal account id, the signed-in email) —
 *    no new backend endpoints in this lift.
 *
 * The Danger zone (Delete workspace) from the design is intentionally OMITTED:
 * it is destructive and needs a backend delete + a confirm flow that are out of
 * scope here.
 */

const THEME_CHOICES: { value: ThemePreference; labelKey: "light" | "system" | "dark" }[] = [
  { value: "light", labelKey: "light" },
  { value: "system", labelKey: "system" },
  { value: "dark", labelKey: "dark" },
];

type WorkspaceState = { kind: "loading" } | { kind: "loaded"; name: string } | { kind: "failed" };
type SaveState = "idle" | "saving" | "saved" | "error";

export default function GeneralTab() {
  const session = useSession();
  const router = useRouter();
  const [theme, setTheme] = useThemePreference();
  const [prefs, updatePref] = usePreferences();
  const t = useTranslations("settings.general");

  const workspaceId = session?.personalAccountId ?? t("workspaceIdFallback");

  // Workspace name: load + editable. Falls back to the i18n placeholder on
  // load failure (so the field is never empty) and surfaces a calm inline
  // error if the save itself fails. The signed-in email stays in the account
  // chip — this field is a real workspace identity, not a person identity.
  const [ws, setWs] = useState<WorkspaceState>({ kind: "loading" });
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [saveError, setSaveError] = useState<string | null>(null);

  // L3 (#5) — Safe Mode. `null` while loading; the toggle is disabled until the
  // real value arrives so we never optimistically show the wrong mode.
  const [safeMode, setSafeMode] = useState<boolean | null>(null);
  const [safeModeSaving, setSafeModeSaving] = useState(false);

  useEffect(() => {
    let active = true;
    getWorkspace()
      .then((w) => {
        if (!active) return;
        setWs({ kind: "loaded", name: w.name });
        setSafeMode(w.safe_mode ?? true);
      })
      .catch(() => {
        if (active) setWs({ kind: "failed" });
      });
    return () => {
      active = false;
    };
  }, []);

  function chooseSafeMode(next: boolean) {
    if (safeMode === next || safeModeSaving) return;
    const previous = safeMode;
    setSafeMode(next); // optimistic — the control reflects the choice immediately
    setSafeModeSaving(true);
    setWorkspaceSafeMode(next)
      .then((w) => setSafeMode(w.safe_mode ?? next))
      .catch(() => setSafeMode(previous)) // revert on failure
      .finally(() => setSafeModeSaving(false));
  }

  function beginEdit() {
    const current = ws.kind === "loaded" ? ws.name : "";
    setDraft(current);
    setEditing(true);
    setSaveError(null);
    setSaveState("idle");
  }
  function cancelEdit() {
    setEditing(false);
    setSaveError(null);
    setSaveState("idle");
  }
  async function onSaveName(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = draft.trim();
    if (!trimmed || saveState === "saving") return;
    setSaveState("saving");
    setSaveError(null);
    try {
      const updated = await renameWorkspace(trimmed);
      setWs({ kind: "loaded", name: updated.name });
      setSaveState("saved");
      setEditing(false);
    } catch (err) {
      setSaveState("error");
      setSaveError(
        err instanceof ApiError ? t("workspaceNameSaveError") : t("workspaceNameSaveError"),
      );
    }
  }

  const workspaceName =
    ws.kind === "loaded" ? ws.name : ws.kind === "failed" ? t("workspaceNameFallback") : "…";

  function chooseLanguage(value: string) {
    // Keep the local preference in sync (the select reads from it) and apply
    // the locale live: persist the cookie, then refresh so the server re-renders
    // with the new catalog.
    updatePref("language", value);
    const locale: Locale = resolveLocale(value);
    setLocaleCookie(locale);
    // #6 — also persist the workspace's LLM OUTPUT language so generated prose
    // (knowledge notes, decision questions, framing) follows the same language.
    // Best-effort: a failed PATCH must never block the live UI locale switch.
    void setWorkspaceLanguage(locale).catch(() => {});
    router.refresh();
  }

  return (
    <div className="general-tab">
      <p className="general-tab__lede">{t("lede")}</p>

      <section className="settings-field" aria-label={t("workspaceName")}>
        <span className="settings-field__label">{t("workspaceName")}</span>
        {editing ? (
          <form className="settings-field__inline-form" onSubmit={onSaveName}>
            <input
              type="text"
              className="settings-field__input"
              aria-label={t("workspaceName")}
              value={draft}
              maxLength={255}
              onChange={(e) => setDraft(e.target.value)}
              // biome-ignore lint/a11y/noAutofocus: focus the one editable field on open
              autoFocus
              disabled={saveState === "saving"}
            />
            <button
              type="submit"
              className="settings-field__primary"
              disabled={!draft.trim() || saveState === "saving"}
            >
              {saveState === "saving" ? t("workspaceNameSaving") : t("workspaceNameSave")}
            </button>
            <button
              type="button"
              className="settings-field__secondary"
              onClick={cancelEdit}
              disabled={saveState === "saving"}
            >
              {t("workspaceNameCancel")}
            </button>
            {saveError && (
              <span className="settings-field__error" aria-live="polite">
                {saveError}
              </span>
            )}
          </form>
        ) : (
          <span className="settings-field__value-with-action">
            <span className="settings-field__value">{workspaceName}</span>
            <button
              type="button"
              className="settings-field__secondary"
              onClick={beginEdit}
              disabled={ws.kind === "loading"}
            >
              {t("workspaceNameEdit")}
            </button>
          </span>
        )}
      </section>

      <section className="settings-field" aria-label={t("safeMode")}>
        <span className="settings-field__label">{t("safeMode")}</span>
        <fieldset className="theme-segmented" disabled={safeMode === null || safeModeSaving}>
          <legend className="theme-segmented__legend">{t("safeMode")}</legend>
          {[
            { value: true, labelKey: "safeModeSafe" as const },
            { value: false, labelKey: "safeModeAuto" as const },
          ].map((choice) => {
            const selected = safeMode === choice.value;
            return (
              <label
                key={String(choice.value)}
                className={`theme-segmented__option${
                  selected ? " theme-segmented__option--on" : ""
                }`}
              >
                <input
                  type="radio"
                  name="safe-mode"
                  className="theme-segmented__input"
                  checked={selected}
                  onChange={() => chooseSafeMode(choice.value)}
                />
                {t(choice.labelKey)}
              </label>
            );
          })}
        </fieldset>
        <span className="settings-field__caption">
          {safeMode === false ? t("safeModeAutoCaption") : t("safeModeSafeCaption")}
        </span>
      </section>

      <section className="settings-field">
        <span className="settings-field__label">{t("theme")}</span>
        <fieldset className="theme-segmented">
          <legend className="theme-segmented__legend">{t("theme")}</legend>
          {THEME_CHOICES.map((choice) => {
            const selected = theme === choice.value;
            return (
              <label
                key={choice.value}
                className={`theme-segmented__option${
                  selected ? " theme-segmented__option--on" : ""
                }`}
              >
                <input
                  type="radio"
                  name="theme"
                  className="theme-segmented__input"
                  value={choice.value}
                  checked={selected}
                  onChange={() => setTheme(choice.value)}
                />
                {t(`themeChoice.${choice.labelKey}`)}
              </label>
            );
          })}
        </fieldset>
      </section>

      <section className="settings-field">
        <span className="settings-field__label">{t("language")}</span>
        <div className="settings-field__control">
          <select
            id="pref-language"
            aria-label={t("language")}
            className="settings-field__select"
            value={prefs.language}
            onChange={(e) => chooseLanguage(e.target.value)}
          >
            {LANGUAGE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
          <span className="settings-field__caption">{t("languageCaption")}</span>
        </div>
      </section>

      <section className="settings-field">
        <span className="settings-field__label">{t("timezone")}</span>
        <select
          id="pref-timezone"
          aria-label={t("timezone")}
          className="settings-field__select"
          value={prefs.timezone}
          onChange={(e) => updatePref("timezone", e.target.value)}
        >
          {TIMEZONE_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </section>

      <section className="settings-field">
        <span className="settings-field__label">{t("dateFormat")}</span>
        <select
          id="pref-dateformat"
          aria-label={t("dateFormat")}
          className="settings-field__select"
          value={prefs.dateFormat}
          onChange={(e) => updatePref("dateFormat", e.target.value)}
        >
          {DATE_FORMAT_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </section>

      <section className="settings-field" aria-label={t("workspaceId")}>
        <span className="settings-field__label">{t("workspaceId")}</span>
        <code className="settings-field__mono">{workspaceId}</code>
      </section>

      {/* GDPR L1 — Art. 6 legal-basis disclosure. Read-only badge for v1:
          founder-editable UI is a follow-up. Default workspaces ship under
          'contract' (BSVibe's service contract); a future consent-based
          deployment would flip this to 'consent'. */}
      <section className="settings-field" aria-label={t("legalBasis")}>
        <span className="settings-field__label">{t("legalBasis")}</span>
        <span className="settings-field__value settings-field__badge">{t("legalBasisValue")}</span>
      </section>
    </div>
  );
}
