/**
 * Client display preferences — language + date format.
 *
 * `language` and `dateFormat` persist to `localStorage` under `bsvibe.preferences`
 * as a single JSON blob (partial updates merge cleanly; a corrupt blob degrades
 * to defaults rather than throwing). `language` also drives the workspace: the
 * General tab PATCHes `workspaces.language` first (the source of truth for BOTH
 * server-rendered content AND the UI chrome, via the locale cookie) and mirrors
 * it here so the select reflects the choice; the app IS translated once i18n
 * landed (#528). `dateFormat` is pure local display.
 *
 * `timezone` is NOT here — it lives on the workspace row (`workspaces.timezone`,
 * N1b), because the server-side NotifyWorker reads it to evaluate quiet hours and
 * a localStorage-only value is invisible to the server. The General tab reads it
 * from GET /api/v1/workspace and writes it via PATCH (`setWorkspaceTimezone`).
 */

export const PREF_STORAGE_KEY = "bsvibe.preferences";

export interface DisplayPreferences {
  /** UI language. Mirrors `workspaces.language`; drives content + chrome (#528). */
  language: string;
  /** Date display format key (pure local display). */
  dateFormat: string;
}

export interface PrefOption {
  value: string;
  label: string;
}

export const LANGUAGE_OPTIONS: PrefOption[] = [
  { value: "en", label: "English" },
  { value: "ko", label: "한국어 (Korean)" },
];

export const TIMEZONE_OPTIONS: PrefOption[] = [
  { value: "Asia/Seoul", label: "Asia/Seoul (UTC+9)" },
  { value: "UTC", label: "UTC" },
  { value: "America/New_York", label: "America/New York (Eastern)" },
  { value: "America/Los_Angeles", label: "America/Los Angeles (Pacific)" },
  { value: "Europe/London", label: "Europe/London" },
];

/** The client-side pre-load placeholder for the time-zone select, shown while
 *  GET /api/v1/workspace resolves. The STORED value lives on the workspace row
 *  and defaults to "UTC" server-side; this is only the display default the PWA
 *  falls back to before the real value arrives. */
export const DEFAULT_TIMEZONE = "Asia/Seoul";

export const DATE_FORMAT_OPTIONS: PrefOption[] = [
  { value: "iso", label: "2026-05-24 (ISO)" },
  { value: "us", label: "05/24/2026 (US)" },
  { value: "eu", label: "24/05/2026 (EU)" },
  { value: "long", label: "May 24, 2026" },
];

export const DEFAULT_PREFERENCES: DisplayPreferences = {
  language: "en",
  dateFormat: "iso",
};

const DEFAULTS = DEFAULT_PREFERENCES;

/** The current preferences, merged over defaults. Corrupt blob → defaults. */
export function getPreferences(): DisplayPreferences {
  if (typeof window === "undefined") return { ...DEFAULTS };
  try {
    const raw = window.localStorage.getItem(PREF_STORAGE_KEY);
    if (!raw) return { ...DEFAULTS };
    const parsed = JSON.parse(raw) as Partial<DisplayPreferences>;
    return {
      language: typeof parsed.language === "string" ? parsed.language : DEFAULTS.language,
      dateFormat: typeof parsed.dateFormat === "string" ? parsed.dateFormat : DEFAULTS.dateFormat,
    };
  } catch {
    return { ...DEFAULTS };
  }
}

/** Update one preference field, preserving the others. */
export function setPreference<K extends keyof DisplayPreferences>(
  key: K,
  value: DisplayPreferences[K],
): void {
  if (typeof window === "undefined") return;
  const next = { ...getPreferences(), [key]: value };
  window.localStorage.setItem(PREF_STORAGE_KEY, JSON.stringify(next));
}
