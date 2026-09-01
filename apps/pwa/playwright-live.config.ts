import { defineConfig, devices } from "@playwright/test";
import { resolveBackendUrl } from "./e2e-live/guard";

/**
 * Browser E2E **behind the auth gate**.
 *
 * `playwright.config.ts` (the suite CI runs) points the PWA at an unreachable
 * backend on purpose, so it can only ever judge the pre-auth surface and the
 * gate's redirect — by construction it cannot see whether anything works once
 * a founder is actually signed in. This config covers exactly that half.
 *
 * It is NOT part of CI. It needs a docker stack and a real SSO account, and
 * pretending otherwise would either leak credentials into CI or produce a
 * suite that is red for reasons nobody acts on. It is run deliberately:
 *
 *   export BSVIBE_E2E_KMS_KEY_B64=$(openssl rand -base64 32)
 *   set -a; . ./deploy/.env.prod; set +a        # Supabase IdP settings
 *   docker compose -f deploy/compose.yaml -f deploy/compose.e2e-live.yaml \
 *     up -d postgres redis backend
 *   export BSVIBE_E2E_EMAIL=… BSVIBE_E2E_PASSWORD=…
 *   cd apps/pwa && pnpm test:e2e:live
 *
 * The guard that keeps this off production lives in `e2e-live/guard.ts` and is
 * unit-tested by CI (`test/e2e-live-guard.test.ts`).
 */

/** Distinct from 3700 (`pnpm dev`, and the prod PWA container) and 3799 (the pre-auth suite). */
const PORT = Number(process.env.BSVIBE_E2E_PORT ?? 3798);
const BASE_URL = `http://127.0.0.1:${PORT}`;

// Resolved (and refused if remote) at config load — before Playwright does
// anything at all. `global-setup.ts` then proves it is the disposable stack.
const BACKEND_URL = resolveBackendUrl(process.env);

export default defineConfig({
  testDir: "./e2e-live",
  // Kept out of the default `testDir` so `pnpm test:e2e` in CI never picks
  // these up — they would fail there for want of a backend.
  globalSetup: "./e2e-live/global-setup.ts",
  // A real sign-in is not free; a retry hides a flake instead of showing it.
  retries: 0,
  // One backend, one account, one browser at a time — parallel sign-ins to the
  // same GoTrue account invite rate limiting that reads as a product bug.
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: BASE_URL,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    // A production build, for the same reason the pre-auth config uses one:
    // under `next dev` the gated routes never finish hydrating, so `useHydrated()`
    // stays false and `RequireAuth` never runs its effect. A dev-mode suite
    // would report defects the product does not have.
    command: `pnpm exec next build && pnpm exec next start --port ${PORT}`,
    url: BASE_URL,
    reuseExistingServer: true,
    timeout: 300_000,
    env: { NEXT_PUBLIC_BACKEND_URL: BACKEND_URL },
  },
});
