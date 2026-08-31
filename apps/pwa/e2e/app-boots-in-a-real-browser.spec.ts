import { expect, test } from "@playwright/test";

/**
 * 앱이 **진짜 브라우저에서** 뜬다.
 *
 * vitest 112개가 못 보는 것을 본다. 그것들은 jsdom 안에서 컴포넌트를 직접
 * 렌더하므로 Next.js 라우팅·레이아웃 조립·하이드레이션을 건너뛴다 — 빌드가
 * 통과하고 컴포넌트 테스트가 전부 green 인데 브라우저에서는 흰 화면인 상태가
 * 이 레포에서 구조적으로 가능했다.
 *
 * 판정은 "body 가 있다" 같은 것이면 안 된다. 흰 화면도 body 는 있다. **제품에만
 * 있는 것**을 본다 — 로그인 폼의 실제 컨트롤들.
 */

test.describe("로그인 화면", () => {
  test("실제 폼 컨트롤이 보인다 — 흰 화면이 아니다", async ({ page }) => {
    await page.goto("/login");

    // 접근성 역할로 찾는다. 클래스명은 리팩터에 부서지고, 텍스트만 보면
    // 로케일 변경에 부서진다. 역할 + 라벨이 사용자가 보는 것에 가장 가깝다.
    await expect(page.getByRole("heading", { name: /sign in/i })).toBeVisible();
    await expect(page.getByLabel(/email/i)).toBeVisible();
    await expect(page.getByLabel("Password", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: /continue$/i })).toBeVisible();
  });

  test("소셜 로그인 진입점이 렌더된다", async ({ page }) => {
    await page.goto("/login");

    await expect(page.getByRole("button", { name: /google/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /github/i })).toBeVisible();
  });

  test("하이드레이션이 터지지 않는다", async ({ page }) => {
    const fatal: string[] = [];
    page.on("pageerror", (err) => fatal.push(String(err)));
    page.on("console", (msg) => {
      if (msg.type() !== "error") return;
      const text = msg.text();
      // 백엔드는 의도적으로 도달 불가능하다(설정 참조) — 그 실패는 이 판정의
      // 대상이 아니다. 여기서 보는 것은 React/Next 자체가 깨지는 것뿐이다.
      if (/Failed to fetch|ERR_CONNECTION|NetworkError|net::/i.test(text)) return;
      fatal.push(text);
    });

    await page.goto("/login");
    await expect(page.getByRole("button", { name: /continue$/i })).toBeVisible();
    // 폼이 실제로 동작하는 상태인지 — 하이드레이션이 끝나야 입력이 먹는다.
    await page.getByLabel(/email/i).fill("probe@example.invalid");
    await expect(page.getByLabel(/email/i)).toHaveValue("probe@example.invalid");

    expect(fatal, `브라우저가 치명 오류를 뱉었다:\n${fatal.join("\n")}`).toEqual([]);
  });
});

test.describe("인증 게이트", () => {
  // 게이트를 통과하면 안 되는 경로들. 하나만 보면 그 하나만 지켜진다.
  for (const path of ["/brief", "/knowledge", "/products", "/settings"]) {
    test(`미인증 상태로 ${path} 에 가면 /login 으로 보내진다`, async ({ page }) => {
      await page.goto(path);

      // `RequireAuth` 는 하이드레이션 후 effect 에서 replace 한다 — 서버
      // 스냅샷(null)으로 성급히 리다이렉트하지 않기 위해서다. 그래서 판정은
      // 최종 URL 이지 첫 응답이 아니다.
      await expect(page).toHaveURL(/\/login$/);
      await expect(page.getByRole("button", { name: /continue$/i })).toBeVisible();
    });
  }

  test("게이트가 보호 콘텐츠를 먼저 비추지 않는다", async ({ page }) => {
    // 리다이렉트 전에 앱 셸이 한 프레임이라도 보이면 미인증 사용자에게
    // 내부 구조가 노출된다. `RequireAuth` 는 그래서 스플래시를 렌더한다.
    await page.goto("/brief");
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByRole("navigation")).toHaveCount(0);
  });
});
