"use client";

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

export default function GeneralTab() {
  const session = useSession();
  const router = useRouter();
  const [theme, setTheme] = useThemePreference();
  const [prefs, updatePref] = usePreferences();
  const t = useTranslations("settings.general");

  const workspaceId = session?.personalAccountId ?? t("workspaceIdFallback");
  // The session has no workspace-name field yet — using the founder's email as
  // a workspace name surface read as "this app thinks my address IS my
  // workspace," which /impeccable audit flagged as confusing for new users.
  // Show the i18n fallback (en: "Your workspace" / ko: "내 워크스페이스") until
  // a real workspace-name field lands. The signed-in email is already surfaced
  // in the account chip at the bottom of the rail / topbar, so no info is lost.
  const workspaceName = t("workspaceNameFallback");

  function chooseLanguage(value: string) {
    // Keep the local preference in sync (the select reads from it) and apply
    // the locale live: persist the cookie, then refresh so the server re-renders
    // with the new catalog.
    updatePref("language", value);
    const locale: Locale = resolveLocale(value);
    setLocaleCookie(locale);
    router.refresh();
  }

  return (
    <div className="general-tab">
      <p className="general-tab__lede">{t("lede")}</p>

      <section className="settings-field" aria-label={t("workspaceName")}>
        <span className="settings-field__label">{t("workspaceName")}</span>
        <span className="settings-field__value">{workspaceName}</span>
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
