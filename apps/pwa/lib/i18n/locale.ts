/**
 * Locale cookie controller (client side).
 *
 * The active UI locale lives in the `bsvibe.locale` cookie (non-routed i18n).
 * The General → Language control writes it here; the next server render reads
 * it in `i18n/request.ts` and supplies the matching message catalog. After
 * writing the cookie the caller triggers a re-render (`router.refresh()`) so
 * the new catalog applies live without a full reload.
 */

import { DEFAULT_LOCALE, LOCALE_COOKIE, type Locale, resolveLocale } from "./config";

/** One year, so the choice survives the session. */
const COOKIE_MAX_AGE = 60 * 60 * 24 * 365;

/** Read the active locale from the cookie (defaults when unset/unknown). */
export function getLocaleCookie(): Locale {
  if (typeof document === "undefined") return DEFAULT_LOCALE;
  const match = document.cookie.split("; ").find((row) => row.startsWith(`${LOCALE_COOKIE}=`));
  return resolveLocale(match?.split("=")[1]);
}

/** Persist the chosen locale to the cookie (path=/, SameSite=Lax). */
export function setLocaleCookie(locale: Locale): void {
  if (typeof document === "undefined") return;
  document.cookie = `${LOCALE_COOKIE}=${locale}; path=/; max-age=${COOKIE_MAX_AGE}; SameSite=Lax`;
}
