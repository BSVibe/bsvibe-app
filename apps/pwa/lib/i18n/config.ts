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

/**
 * Pick the best SUPPORTED locale from a browser `Accept-Language` header, used
 * on a first visit (no `bsvibe.locale` cookie) so the UI auto-follows the user's
 * region — a Korean browser lands on Korean, everyone else on the English
 * default. The explicit General → Language cookie always overrides this. Parses
 * the quality-ordered tag list ("ko-KR,ko;q=0.9,en;q=0.8") and returns the first
 * whose base language is supported; falls back to the default when none match.
 */
export function resolveLocaleFromHeader(header: string | undefined | null): Locale {
  if (!header) return DEFAULT_LOCALE;
  const tags = header
    .split(",")
    .map((part) => part.split(";")[0]?.trim().toLowerCase())
    .filter(Boolean);
  for (const tag of tags) {
    const base = tag.split("-")[0];
    if ((LOCALES as readonly string[]).includes(base)) return base as Locale;
  }
  return DEFAULT_LOCALE;
}
