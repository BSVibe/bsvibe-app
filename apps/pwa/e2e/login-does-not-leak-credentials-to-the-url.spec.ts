import { expect, test } from "@playwright/test";

/**
 * 하이드레이션 전에 제출해도 **비밀번호가 URL 에 실리지 않는다** (#1053).
 *
 * 2026-09-23 실측. Playwright 로 prod 로그인 폼을 채우고 제출했더니 브라우저가
 * 이 URL 로 이동했다:
 *
 *     https://app.bsvibe.dev/login?email=…&password=…
 *
 * `<form onSubmit={handleSubmit}>` 에 `action`/`method` 가 없어서, React 가
 * 하이드레이션을 끝내기 전에 제출되면 그 핸들러가 아직 안 붙어 있고 브라우저가
 * **기본 동작(같은 URL 로 GET)** 을 한다 — 모든 `name` 필드를 쿼리에 실어서.
 *
 * ⚠️ 테스트 하네스 문제가 아니다. 느린 연결 + 엔터로 제출하는 사용자면 실제로
 * 난다. 그리고 그 URL 은 **브라우저 히스토리 · Referer · 액세스 로그**에 남는다.
 *
 * 왜 여기(사전 인증 스위트)인가 — **진짜 비밀번호가 필요 없다.** 명제는 "로그인이
 * 되는가"가 아니라 "값이 어디로 가는가"다. 더미 값으로 충분하고, 그래서 이 테스트는
 * 자격증명을 일절 안 만진다.
 *
 * 왜 vitest 로는 못 잡나 — jsdom 은 하이드레이션 이전/이후를 구분하지 않고,
 * 폼의 **브라우저 기본 제출**을 재현하지 않는다. 이건 진짜 브라우저에서만 보인다.
 */

/** 절대 로그에 남아선 안 되는, 알아보기 쉬운 더미. */
const DUMMY_PASSWORD = "not-a-real-password-1053";
const DUMMY_EMAIL = "repro-1053@example.invalid";

test.describe("로그인 폼 — 자격증명이 URL 로 새지 않는다", () => {
  test("하이드레이션 전에 제출해도 쿼리스트링에 안 실린다", async ({ page }) => {
    // 하이드레이션을 **막는다.** 스크립트가 안 오면 onSubmit 핸들러도 안 붙고,
    // 브라우저는 폼의 기본 동작을 한다 — 사용자가 느린 연결에서 겪는 바로 그 상태.
    await page.route("**/*.js", (route) => route.abort());

    await page.goto("/login", { waitUntil: "domcontentloaded" });

    // SSR 된 HTML 에 폼이 있다(흰 화면이면 아래가 실패해서 그것도 알려준다).
    await page.locator("#email").fill(DUMMY_EMAIL);
    await page.locator("#password").fill(DUMMY_PASSWORD);
    await page.locator("#password").press("Enter");

    // 네비게이션이 일어나든 안 일어나든, **URL 에 비밀번호가 있으면 안 된다.**
    await page.waitForTimeout(1000);
    const url = page.url();
    expect(
      url,
      `비밀번호가 URL 에 실렸다: ${url.replace(DUMMY_PASSWORD, "<LEAKED>")}`,
    ).not.toContain(DUMMY_PASSWORD);
    // 이메일도 마찬가지다 — 개인정보고, 같은 경로로 샌다.
    expect(url).not.toContain(encodeURIComponent(DUMMY_EMAIL));
  });

  test("대조군 — 하이드레이션이 되면 폼은 정상 동작한다", async ({ page }) => {
    // 위 테스트가 "폼이 아예 안 뜬다"로 통과하는 것을 막는다.
    await page.goto("/login");
    await expect(page.locator("#email")).toBeVisible();
    await expect(page.locator("#password")).toBeVisible();
    await expect(page.locator('form button[type="submit"]')).toBeEnabled();
  });
});
