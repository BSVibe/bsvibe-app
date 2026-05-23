import { themeBootScript } from "@/lib/theme/theme";
import type { Metadata, Viewport } from "next";
import { NextIntlClientProvider } from "next-intl";
import { getLocale } from "next-intl/server";
import type { ReactNode } from "react";
import "./globals.css";

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
