/** PWA manifest — produced at /manifest.webmanifest at request time.
 *
 * Keeps the brand colors and the post-auth start surface in sync with the
 * surrounding chrome (the `<meta name="theme-color">` value, the brand mark
 * `app/icon.svg`, the `--bg` token in `globals.css`). The browser caches the
 * manifest by URL; values change in one place here, not in three.
 */

import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "BSVibe",
    short_name: "BSVibe",
    description: "AI Agent OS",
    // The Brief is the post-auth home — anyone tapping the home-screen icon
    // wants to see what their agent is working on. Auth gate handles the rest.
    start_url: "/brief",
    scope: "/",
    display: "standalone",
    // Matches the `<meta name="theme-color">` value emitted by the root layout
    // and the `--bg` token in globals.css. The icon mark (`app/icon.svg`)
    // reads against this neutral surface.
    background_color: "#fbfbfa",
    theme_color: "#fbfbfa",
    icons: [
      {
        // Single SVG icon — scales for every required size. Resolved by Next's
        // file-conventions handling of `app/icon.svg`.
        src: "/icon.svg",
        sizes: "any",
        type: "image/svg+xml",
      },
    ],
  };
}
