import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const require = createRequire(import.meta.url);
// Resolve the REAL @testing-library/react entry so the shim can re-export it
// without recursing back through the alias below.
const rtlActual = require.resolve("@testing-library/react");

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: [
      // Escape hatch the shim (test/i18n-render.tsx) uses to reach the real
      // library — must come before the bare-name alias below.
      { find: "@rtl-actual", replacement: rtlActual },
      // Every test that imports { render } from @testing-library/react gets the
      // NextIntlClientProvider-wrapped render from the shim, so useTranslations
      // resolves under vitest without rewriting test imports.
      {
        find: /^@testing-library\/react$/,
        replacement: fileURLToPath(new URL("./test/i18n-render.tsx", import.meta.url)),
      },
      { find: "@", replacement: fileURLToPath(new URL("./", import.meta.url)) },
    ],
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["test/**/*.test.{ts,tsx}"],
  },
});
