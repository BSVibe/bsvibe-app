import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

// The browser calls the backend directly at NEXT_PUBLIC_BACKEND_URL
// (cross-origin; the backend serves CORS). The former `/api/:path*` →
// backend rewrite proxy was retired because Cloudflare bot-challenged the
// server-side proxy. See lib/api/client.ts for the base-prefix logic.
const config: NextConfig = {
  reactStrictMode: true,
  // The "Inside" surface was relabeled "Knowledge" and moved to /knowledge.
  // Keep any old /inside link (bookmark, external ref) working.
  async redirects() {
    return [{ source: "/inside", destination: "/knowledge", permanent: true }];
  },
  experimental: {
    // Tree-shake barrel imports from libraries that ship a single index
    // entry containing the whole API surface. Without this, importing one
    // function from ``next-intl`` pulls in the whole runtime even though we
    // only use the React + server bindings. ``d3-force`` and
    // ``react-force-graph-2d`` are the Knowledge graph view's heavy
    // dependencies — already lazy-loaded via ``next/dynamic``, but the
    // inner-tree-shake here keeps THAT chunk lean too.
    //
    // Turbopack supports this flag in Next 16. The /impeccable audit's
    // cold-load measurement was 3.4s DCL on /login; alongside the
    // <link rel="preconnect"> emitted from the root layout (PR #170) this
    // is one of the cheapest levers we can pull without restructuring the
    // route graph.
    optimizePackageImports: ["next-intl", "d3-force", "react-force-graph-2d"],
  },
};

// next-intl WITHOUT i18n routing: the request config (i18n/request.ts) picks
// the locale from the `bsvibe.locale` cookie. No `[locale]` URL segment.
const withNextIntl = createNextIntlPlugin("./i18n/request.ts");

export default withNextIntl(config);
