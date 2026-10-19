import { type Page, expect, test } from "@playwright/test";
import { signIn } from "./helpers/live-login";

/**
 * #1042 leftovers, seen in a real browser (jsdom has no layout), plus the floating "Request"
 * button against the last line of a page.
 *
 * A fresh stack has no product and no runs, so the two LIST reads the product page composes
 * itself from (`/api/v1/products`, `/api/v1/runs`) are answered here — the shape prod showed
 * on 2026-09-24: one batch of 27 cancelled runs in the same minute, and a run with no title.
 */

const SHOTS = process.env.BSVIBE_E2E_SHOTS_DIR;
const PRODUCT_ID = "00000000-0000-4000-8000-000000000042";

async function shot(page: Page, name: string, fullPage = true): Promise<void> {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage });
}

async function seedProduct(page: Page): Promise<void> {
  const now = new Date();
  now.setSeconds(0, 0);
  const iso = (offsetS: number) => new Date(now.getTime() + offsetS * 1000).toISOString();
  await page.route("**/api/v1/products", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const res = await route.fetch();
    const ws = ((await res.json()) as { workspace_id?: string }[])[0]?.workspace_id ?? "ws";
    await route.fulfill({
      response: res,
      json: [
        {
          id: PRODUCT_ID,
          workspace_id: ws,
          name: "toolkit",
          slug: "toolkit",
          repo_url: null,
          bootstrap_status: null,
          bootstrap_artifacts_count: null,
          bootstrap_error: null,
          bootstrap_progress: null,
          created_at: iso(-86400),
          updated_at: iso(0),
        },
      ],
    });
  });
  await page.route("**/api/v1/runs?*", async (route) => {
    const run = (i: number, over: Record<string, unknown> = {}) => ({
      id: `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`,
      workspace_id: "ws",
      product_id: PRODUCT_ID,
      request_id: null,
      status: "cancelled",
      intent: `배치 작업 ${i}`,
      created_at: iso(-i),
      updated_at: iso(-i),
      ...over,
    });
    await route.fulfill({
      json: [
        ...Array.from({ length: 27 }, (_, i) => run(i + 1)),
        run(100, {
          intent: null,
          status: "failed",
          updated_at: iso(-7200),
          created_at: iso(-7200),
        }),
      ],
    });
  });
}

test.use({ colorScheme: "dark", viewport: { width: 760, height: 1000 } });

test("활동 — 한 배치는 한 줄, 제목 없는 런은 '제목 없음'", async ({ page }) => {
  await seedProduct(page);
  await signIn(page);
  await page.goto("/products/toolkit");
  const batch = page.locator("details.product-runs__batch");
  await expect(batch).toHaveCount(1);
  await expect(batch.locator("summary")).toContainText("27");
  await expect(page.locator(".product-run__title--none")).toHaveCount(1);
  await shot(page, "activity");

  await batch.locator("summary").click();
  await expect(batch.locator("li.product-run")).toHaveCount(27);
});

/** Distance from the tab row to the first section label under it. */
async function gapBelowTabs(page: Page): Promise<number> {
  return page.evaluate(() => {
    const tabs = document.querySelector(".product-tabs")?.getBoundingClientRect();
    const label = [...document.querySelectorAll(".section-label")].find(
      (el) => el.getBoundingClientRect().top > (tabs?.bottom ?? 0),
    );
    return tabs && label ? label.getBoundingClientRect().top - tabs.bottom : -1;
  });
}

test("탭 줄과 첫 섹션 사이 간격은 탭마다 같다 (#1042 §4)", async ({ page }) => {
  await seedProduct(page);
  await signIn(page);
  await page.goto("/products/toolkit");
  await expect(page.locator(".product-runs")).toBeVisible();
  const activity = await gapBelowTabs(page);
  await page.getByRole("tab", { name: /settings|설정/i }).click();
  await expect(page.locator(".product-resources")).toBeVisible();
  const settings = await gapBelowTabs(page);
  expect(activity).toBeGreaterThan(0);
  // Was 34px apart. The ~6px left is the settings header row: its label sits on the baseline
  // of the "Add resource" button beside it, which the activity tab's bare label does not have.
  expect(Math.abs(settings - activity)).toBeLessThanOrEqual(8);
});

test("설정 — 위험 구역은 바인딩과 떨어져 있다", async ({ page }) => {
  await seedProduct(page);
  await signIn(page);
  await page.goto("/products/toolkit");
  await page.getByRole("tab", { name: /settings|설정/i }).click();
  const danger = page.locator(".product-danger");
  await expect(danger).toBeVisible();
  const gap = await page.evaluate(() => {
    const above = document.querySelector(".product-bindings")?.getBoundingClientRect();
    const below = document.querySelector(".product-danger")?.getBoundingClientRect();
    return above && below ? below.top - above.bottom : -1;
  });
  expect(gap).toBeGreaterThanOrEqual(40);
  await page.waitForLoadState("networkidle");
  await expect(page.getByText(/Loading (resources|bindings)/i)).toHaveCount(0);
  await shot(page, "settings");
});

test("떠 있는 요청 버튼은 맨 아래로 스크롤해도 마지막 줄을 가리지 않는다", async ({ page }) => {
  // A connected push channel makes the notifications page long enough to scroll — the shape
  // the founder saw. Without it the page fits the viewport and the check proves nothing.
  await page.route("**/api/v1/notifications/prefs", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const res = await route.fetch();
    await route.fulfill({
      response: res,
      json: { ...(await res.json()), available_channels: ["in_app", "telegram"] },
    });
  });
  await signIn(page);
  for (const path of ["/settings/notifications", "/settings/schedules", "/settings/general"]) {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.waitForTimeout(200);
    const overlap = await page.evaluate(() => {
      const fab = document.querySelector(".direct-fab")?.getBoundingClientRect();
      const main = document.querySelector(".shell__main");
      if (!fab || !main) return null;
      // The lowest piece of real content (leaf elements with text or a control).
      let lowest = 0;
      for (const el of main.querySelectorAll("p, label, button, input, li, h2, a")) {
        const r = el.getBoundingClientRect();
        if (r.height > 0 && r.bottom > lowest) lowest = r.bottom;
      }
      return { lowest, fabTop: fab.top };
    });
    await shot(page, `fab-${path.split("/").pop()}`, false);
    if (path.endsWith("notifications")) {
      const scrolled = await page.evaluate(() => window.scrollY);
      expect(scrolled, "the notifications page must actually scroll").toBeGreaterThan(0);
    }
    expect(overlap, path).not.toBeNull();
    expect(overlap?.lowest, path).toBeLessThanOrEqual(overlap?.fabTop ?? 0);
  }
});
