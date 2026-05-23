/**
 * Client display preferences — language, time zone, date format.
 *
 * These are LOCAL-ONLY for now: persisted to `localStorage` under
 * `bsvibe.preferences`, no backend sync (a documented follow-up). The language
 * choice does NOT translate the app yet — real i18n lands in a later lift; the
 * General tab surfaces a caption saying so.
 *
 * Stored as a single JSON blob so partial updates merge cleanly and a corrupt
 * blob degrades to defaults rather than throwing.
 */

export const PREF_STORAGE_KEY = "bsvibe.preferences";

export interface DisplayPreferences {
  /** UI language (does not translate the app until i18n lands). */
  language: string;
  /** IANA time zone used when we render times. */
  timezone: string;
  /** Date display format key. */
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

export const DATE_FORMAT_OPTIONS: PrefOption[] = [
  { value: "iso", label: "2026-05-24 (ISO)" },
  { value: "us", label: "05/24/2026 (US)" },
  { value: "eu", label: "24/05/2026 (EU)" },
  { value: "long", label: "May 24, 2026" },
];

export const DEFAULT_PREFERENCES: DisplayPreferences = {
  language: "en",
  timezone: "Asia/Seoul",
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
      timezone: typeof parsed.timezone === "string" ? parsed.timezone : DEFAULTS.timezone,
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
