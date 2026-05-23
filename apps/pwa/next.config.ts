import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

// The browser calls the backend directly at NEXT_PUBLIC_BACKEND_URL
// (cross-origin; the backend serves CORS). The former `/api/:path*` →
// backend rewrite proxy was retired because Cloudflare bot-challenged the
// server-side proxy. See lib/api/client.ts for the base-prefix logic.
const config: NextConfig = {
  reactStrictMode: true,
};

// next-intl WITHOUT i18n routing: the request config (i18n/request.ts) picks
// the locale from the `bsvibe.locale` cookie. No `[locale]` URL segment.
const withNextIntl = createNextIntlPlugin("./i18n/request.ts");

export default withNextIntl(config);
