import { expect, test } from "@playwright/test";
import { signIn } from "./helpers/live-login";

/**
 * What happens **after** a real sign-in.
 *
 * The 112 vitest component tests render components directly in jsdom, and the
 * pre-auth browser suite points at a dead backend. Neither can see any of the
 * propositions below: they all require a session that a real GoTrue actually
 * issued, carried by a real browser across real navigations.
 *
 * Every test signs in from scratch. Sharing a `storageState` would be faster
 * and would quietly stop testing the thing most worth testing — that signing
 * in works, repeatedly, from a cold browser.
 */

test.describe("인증된 셸", () => {
  test("실제 폼으로 로그인하면 앱 셸이 뜬다 — 스플래시에 갇히지 않는다", async ({ page }) => {
    await signIn(page);

    // The pre-auth suite asserts this is ABSENT when gated. The gate opening
    // has to mean the opposite, or "opened" would be indistinguishable from
    // "redirected somewhere else that also has no nav".
    await expect(page.getByRole("navigation").first()).toBeVisible();
  });

  test("새로고침해도 세션이 산다", async ({ page }) => {
    await signIn(page);
    await page.reload();

    // A session held only in a JS variable survives every component test and
    // dies here. This is the assertion jsdom structurally cannot make.
    await expect(page).toHaveURL(/\/brief$/);
    await expect(page.getByRole("navigation").first()).toBeVisible();
  });

  // The same routes the pre-auth suite proves are CLOSED. Proving they open
  // for a signed-in founder is what makes that suite's result meaningful —
  // otherwise a gate that refused everyone would pass both.
  for (const path of ["/knowledge", "/products", "/settings"]) {
    test(`로그인 상태로 ${path} 에 직접 가면 통과한다`, async ({ page }) => {
      await signIn(page);
      await page.goto(path);

      await expect(page).toHaveURL(new RegExp(`${path}$`));
      await expect(page).not.toHaveURL(/\/login$/);
      await expect(page.getByRole("navigation").first()).toBeVisible();
    });
  }

  test("인증된 화면에서 하이드레이션이 터지지 않는다", async ({ page }) => {
    const fatal: string[] = [];
    page.on("pageerror", (err) => fatal.push(String(err)));
    page.on("console", (msg) => {
      if (msg.type() === "error") fatal.push(msg.text());
    });

    await signIn(page);
    await expect(page.getByRole("navigation").first()).toBeVisible();

    // Unlike the pre-auth suite, network errors are NOT filtered out here.
    // The backend is real and reachable, so a failed fetch behind the gate is
    // a genuine finding rather than the configured-unreachable noise.
    expect(fatal, `브라우저가 오류를 뱉었다:\n${fatal.join("\n")}`).toEqual([]);
  });

  test("로그아웃하면 게이트가 다시 닫힌다", async ({ page }) => {
    await signIn(page);

    await page.goto("/settings");
    await page
      .getByRole("button", { name: /sign out/i })
      .first()
      .click();

    // Sign-out has to actually clear the session, not just navigate away — so
    // the judgement is a fresh attempt at a gated route, not the URL it lands
    // on right after the click.
    await expect(page).toHaveURL(/\/login$/, { timeout: 30_000 });
    await page.goto("/brief");
    await expect(page).toHaveURL(/\/login$/);
  });
});
