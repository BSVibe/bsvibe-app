import { LOCALE_COOKIE, resolveLocale } from "@/lib/i18n/config";
import { getRequestConfig } from "next-intl/server";
import { cookies } from "next/headers";

/**
 * next-intl request config (the "without i18n routing" setup). The active
 * locale is read from the `bsvibe.locale` cookie server-side; messages for that
 * locale are loaded from the catalogs under `messages/`. Returned `locale` +
 * `messages` flow into `NextIntlClientProvider` via the root layout.
 */
export default getRequestConfig(async () => {
  const cookieStore = await cookies();
  const locale = resolveLocale(cookieStore.get(LOCALE_COOKIE)?.value);

  return {
    locale,
    messages: (await import(`../messages/${locale}.json`)).default,
  };
});
