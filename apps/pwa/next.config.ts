import type { NextConfig } from "next";

// The browser calls the backend directly at NEXT_PUBLIC_BACKEND_URL
// (cross-origin; the backend serves CORS). The former `/api/:path*` →
// backend rewrite proxy was retired because Cloudflare bot-challenged the
// server-side proxy. See lib/api/client.ts for the base-prefix logic.
const config: NextConfig = {
  reactStrictMode: true,
};

export default config;
