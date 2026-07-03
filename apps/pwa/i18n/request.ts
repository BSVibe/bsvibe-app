import { LOCALE_COOKIE, resolveLocale, resolveLocaleFromHeader } from "@/lib/i18n/config";
import { getRequestConfig } from "next-intl/server";
import { cookies, headers } from "next/headers";

/**
 * next-intl request config (the "without i18n routing" setup). The active
 * locale is the `bsvibe.locale` cookie when set (the General → Language choice);
 * on a FIRST visit with no cookie it auto-detects from the browser's
 * `Accept-Language` header (a Korean browser → Korean, everyone else → the
 * English default). Messages for that locale load from the catalogs under
 * `messages/`; `locale` + `messages` flow into `NextIntlClientProvider` via the
 * root layout.
 */
export default getRequestConfig(async () => {
  const cookieStore = await cookies();
  const cookieLocale = cookieStore.get(LOCALE_COOKIE)?.value;
  // Explicit choice wins; otherwise follow the browser's Accept-Language.
  const locale = cookieLocale
    ? resolveLocale(cookieLocale)
    : resolveLocaleFromHeader((await headers()).get("accept-language"));

  return {
    locale,
    messages: (await import(`../messages/${locale}.json`)).default,
  };
});
