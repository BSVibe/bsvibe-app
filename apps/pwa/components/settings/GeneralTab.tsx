"use client";

import { useSession } from "@/lib/auth/session";
import {
  DATE_FORMAT_OPTIONS,
  LANGUAGE_OPTIONS,
  TIMEZONE_OPTIONS,
} from "@/lib/preferences/preferences";
import { usePreferences } from "@/lib/preferences/usePreferences";
import type { ThemePreference } from "@/lib/theme/theme";
import { useThemePreference } from "@/lib/theme/useTheme";

/**
 * Settings → General. Workspace basics + appearance.
 *
 *  - Theme (the headline): a Light / System / Dark segmented control wired to
 *    the theme controller. Choosing one persists `bsvibe.theme` and applies
 *    `data-theme` to <html> immediately. This is the must-work piece.
 *  - Language / Time zone / Date format: LOCAL-only preferences (no backend yet
 *    — server sync is a follow-up). The language control does not translate the
 *    app until i18n lands; a caption says so.
 *  - Workspace name / Workspace ID: DISPLAY-only. We surface what the client
 *    already knows (the session's personal account id, the signed-in email) —
 *    no new backend endpoints in this lift.
 *
 * The Danger zone (Delete workspace) from the design is intentionally OMITTED:
 * it is destructive and needs a backend delete + a confirm flow that are out of
 * scope here.
 */

const THEME_CHOICES: { value: ThemePreference; label: string }[] = [
  { value: "light", label: "Light" },
  { value: "system", label: "System" },
  { value: "dark", label: "Dark" },
];

export default function GeneralTab() {
  const session = useSession();
  const [theme, setTheme] = useThemePreference();
  const [prefs, updatePref] = usePreferences();

  const workspaceId = session?.personalAccountId ?? "Not available yet";
  const workspaceName = session?.email ?? "Your workspace";

  return (
    <div className="general-tab">
      <p className="general-tab__lede">General — workspace basics.</p>

      <section className="settings-field" aria-label="Workspace name">
        <span className="settings-field__label">Workspace name</span>
        <span className="settings-field__value">{workspaceName}</span>
      </section>

      <section className="settings-field">
        <span className="settings-field__label">Theme</span>
        <fieldset className="theme-segmented">
          <legend className="theme-segmented__legend">Theme</legend>
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
                {choice.label}
              </label>
            );
          })}
        </fieldset>
      </section>

      <section className="settings-field">
        <span className="settings-field__label">Language</span>
        <div className="settings-field__control">
          <select
            id="pref-language"
            aria-label="Language"
            className="settings-field__select"
            value={prefs.language}
            onChange={(e) => updatePref("language", e.target.value)}
          >
            {LANGUAGE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
          <span className="settings-field__caption">
            Saved locally. This doesn&rsquo;t translate the app yet — full i18n is coming.
          </span>
        </div>
      </section>

      <section className="settings-field">
        <span className="settings-field__label">Time zone</span>
        <select
          id="pref-timezone"
          aria-label="Time zone"
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
        <span className="settings-field__label">Date format</span>
        <select
          id="pref-dateformat"
          aria-label="Date format"
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

      <section className="settings-field" aria-label="Workspace ID">
        <span className="settings-field__label">Workspace ID</span>
        <code className="settings-field__mono">{workspaceId}</code>
      </section>
    </div>
  );
}
