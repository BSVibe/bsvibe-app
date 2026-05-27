import { themeBootScript } from "@/lib/theme/theme";
import type { Metadata, Viewport } from "next";
import { NextIntlClientProvider } from "next-intl";
import { getLocale } from "next-intl/server";
import type { ReactNode } from "react";
import "./globals.css";

/** Resolve the same backend origin ``apiFetch`` uses, so we can emit a
 *  ``<link rel="preconnect">`` for it. Build-time constant; an empty value
 *  (vitest / same-origin dev) means no preconnect is emitted. A malformed
 *  ``NEXT_PUBLIC_BACKEND_URL`` is treated as absent rather than crashed on. */
function resolveBackendOrigin(): string | null {
  const raw = process.env.NEXT_PUBLIC_BACKEND_URL;
  if (!raw) return null;
  try {
    return new URL(raw).origin;
  } catch {
    return null;
  }
}

const BACKEND_ORIGIN = resolveBackendOrigin();

export const metadata: Metadata = {
  title: "BSVibe",
  description: "The AI agent OS",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#fbfbfa" },
    { media: "(prefers-color-scheme: dark)", color: "#1c1b19" },
  ],
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  // next-intl (non-routed): the active locale comes from the `bsvibe.locale`
  // cookie via i18n/request.ts. NextIntlClientProvider auto-hydrates messages
  // from that request config to all client components below.
  const locale = await getLocale();

  return (
    <html lang={locale} suppressHydrationWarning>
      <head>
        {/* Kick off the TCP + TLS handshake to the backend origin while the
            rest of the document parses. The first authenticated ``apiFetch``
            after sign-in then reuses the warmed connection instead of paying
            DNS + TCP + TLS on the critical path. ``crossOrigin="anonymous"``
            because the actual API calls go without credentials cookies
            (Authorization bearer header instead); matching the credentials
            mode of the real fetch is what lets the browser actually reuse
            the warmed connection. */}
        {BACKEND_ORIGIN && <link rel="preconnect" href={BACKEND_ORIGIN} crossOrigin="anonymous" />}
        {/* Set the resolved theme before first paint so there's no flash of the
            wrong color scheme. Runs ahead of hydration; see lib/theme/theme.ts. */}
        {/* biome-ignore lint/security/noDangerouslySetInnerHtml: trusted, build-time-constant boot script */}
        <script dangerouslySetInnerHTML={{ __html: themeBootScript }} />
      </head>
      <body>
        <NextIntlClientProvider>{children}</NextIntlClientProvider>
      </body>
    </html>
  );
}
