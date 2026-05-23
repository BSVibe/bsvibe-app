import { themeBootScript } from "@/lib/theme/theme";
import type { Metadata, Viewport } from "next";
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

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* Set the resolved theme before first paint so there's no flash of the
            wrong color scheme. Runs ahead of hydration; see lib/theme/theme.ts. */}
        {/* biome-ignore lint/security/noDangerouslySetInnerHtml: trusted, build-time-constant boot script */}
        <script dangerouslySetInnerHTML={{ __html: themeBootScript }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
