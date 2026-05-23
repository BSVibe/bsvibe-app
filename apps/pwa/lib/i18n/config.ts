/**
 * Locale configuration for the PWA.
 *
 * i18n here is NON-ROUTED: the active locale lives in a cookie
 * (`bsvibe.locale`), not in the URL. There is intentionally no `[locale]`
 * route segment — the General → Language control writes the cookie and the
 * server reads it to choose the message catalog. This matches the existing
 * preference model and avoids restructuring every route.
 */

export const LOCALES = ["en", "ko"] as const;

export type Locale = (typeof LOCALES)[number];

export const DEFAULT_LOCALE: Locale = "en";

/** Cookie that holds the active UI locale. */
export const LOCALE_COOKIE = "bsvibe.locale";

/** Narrow an arbitrary string to a supported locale, falling back to default. */
export function resolveLocale(value: string | undefined | null): Locale {
  if (value && (LOCALES as readonly string[]).includes(value)) {
    return value as Locale;
  }
  return DEFAULT_LOCALE;
}
