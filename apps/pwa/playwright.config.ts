import { defineConfig, devices } from "@playwright/test";

/**
 * 브라우저 E2E — vitest 가 구조적으로 못 보는 것만 본다.
 *
 * 이 레포의 "E2E" 는 지금까지 `docs/e2e/*-checklist.md` **69개의 사람이 읽는
 * 체크리스트**였다. 자동화된 브라우저 검증은 0 이었고, PWA 테스트 112개는 전부
 * vitest 컴포넌트 테스트다 — jsdom 안에서 컴포넌트를 직접 렌더하므로 다음을
 * 구조적으로 볼 수 없다:
 *
 *   * Next.js 라우팅/레이아웃이 실제로 조립되는가 (빌드는 통과하는데 런타임에
 *     흰 화면이 되는 부류)
 *   * 클라이언트 하이드레이션이 터지지 않는가
 *   * 인증 게이트가 **브라우저에서** 실제로 리다이렉트하는가
 *
 * ⚠️ **prod 를 겨누지 않는다.** `webServer` 가 로컬 `next dev` 를 띄우고 거기에만
 * 붙는다. prod 에 Playwright 를 겨누면 실제 데이터가 생긴다.
 *
 * ⚠️ **dev-login 우회를 만들지 않는다** (형님 규칙). 이 설정이 커버하는 것은
 * 인증 이전 표면 + 게이트의 리다이렉트뿐이고, 인증된 플로우는 실제 SSO 테스트
 * 계정으로 로그인하는 별도 스위트가 맡는다.
 */

/** dev 서버와 충돌하지 않는 전용 포트 — 3700 은 사람이 쓰는 `pnpm dev` 몫이다. */
const PORT = Number(process.env.PLAYWRIGHT_PORT ?? 3799);
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  // 실패가 flake 인지 진짜인지 구분하려면 재현이 먼저다 — CI 에서만 1회 재시도.
  retries: process.env.CI ? 1 : 0,
  // 워커 병렬은 dev 서버 하나를 공유하므로 보수적으로.
  workers: process.env.CI ? 1 : undefined,
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? [["github"], ["list"]] : [["list"]],
  use: {
    baseURL: BASE_URL,
    // 실패한 e2e 는 스크린샷 없이는 원격에서 진단할 수 없다.
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    // ⚠️ `next dev` 가 아니라 **프로덕션 빌드**를 띄운다. 이건 취향이 아니라
    //    실측으로 강제된 것이다 (2026-08-31):
    //
    //      dev(Turbopack)  /brief 미인증 → 스플래시에서 영원히 멈춤, 리다이렉트 없음
    //      prod build      /brief 미인증 → /login 으로 리다이렉트 ✅
    //      app.bsvibe.dev  /brief 미인증 → /login 으로 리다이렉트 ✅
    //
    //    dev 모드에서는 게이트된 라우트의 하이드레이션이 끝나지 않아
    //    `useHydrated()` 가 false 로 남고 `RequireAuth` 의 리다이렉트 effect 가
    //    영영 안 돈다. dev 로 스위트를 짰다면 **제품에 없는 결함**을 5건
    //    보고했을 것이고, 반대로 prod 에서만 나는 결함은 못 봤을 것이다.
    //
    //    E2E 는 **배포되는 것**을 봐야 한다. 빌드 비용(CI 에서 ~1분)은 그 대가다.
    command: `pnpm exec next build && pnpm exec next start --port ${PORT}`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 300_000,
    env: {
      // 백엔드는 이 스위트의 대상이 아니다. 도달 불가능한 주소를 주어 어떤
      // 실수로도 prod 백엔드를 건드리지 못하게 한다 — 인증 이전 표면과
      // 리다이렉트는 백엔드 없이도 판정된다.
      NEXT_PUBLIC_BACKEND_URL: "http://127.0.0.1:9",
    },
  },
});
