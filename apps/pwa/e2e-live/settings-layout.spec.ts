import { type Page, expect, test } from "@playwright/test";
import { signIn } from "./helpers/live-login";

/**
 * Settings → 알림 / 예약 — what the founder actually SEES (2026-10-19 screenshots).
 *
 * jsdom has no layout, so the component tests could not see any of this:
 *
 *  - helper sentences sat in a narrow RIGHT-aligned column (`.settings-field__caption` is a
 *    caption for a label|value row, reused as a standalone paragraph);
 *  - the notification events rendered as a browser-default BULLETED list (no CSS at all);
 *  - the schedule form put each input at the far right of its row, the cron caption beside it;
 *  - a long schedule instruction filled the row as one wall of text.
 *
 * The notification matrix only renders with a push channel connected, and a fresh stack has
 * none — so that ONE read is answered here (a telegram channel). Everything else is the real
 * backend, and the schedule is created through the real form.
 */

const SHOTS = process.env.BSVIBE_E2E_SHOTS_DIR;

async function shot(page: Page, name: string): Promise<void> {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
}

test.use({ colorScheme: "dark", viewport: { width: 760, height: 1000 } });

test.describe("settings layout", () => {
  test("알림 — 안내문은 왼쪽 정렬로 폭을 쓰고, 이벤트는 불릿 없는 목록이다", async ({ page }) => {
    await page.route("**/api/v1/notifications/prefs", async (route) => {
      if (route.request().method() !== "GET") return route.continue();
      const res = await route.fetch();
      const body = await res.json();
      await route.fulfill({
        response: res,
        json: { ...body, available_channels: ["in_app", "telegram"] },
      });
    });
    await signIn(page);
    await page.goto("/settings/notifications");
    const events = page.locator(".notifications-events");
    await expect(events).toBeVisible();
    await shot(page, "notifications");

    expect(await events.evaluate((el) => getComputedStyle(el).listStyleType)).toBe("none");
    for (const hint of await page
      .locator(".notifications-matrix > p, .notifications-quiet > p")
      .all()) {
      const style = await hint.evaluate((el) => {
        const s = getComputedStyle(el);
        return { align: s.textAlign, width: el.getBoundingClientRect().width };
      });
      expect(style.align).not.toBe("right");
      expect(style.width).toBeGreaterThan(400);
    }
  });

  test("예약 — 입력칸은 라벨 아래 전체 폭, 긴 지시문은 접힌다", async ({ page }) => {
    await signIn(page);
    await page.goto("/settings/schedules");
    const instruction = page.locator(".schedules-form__instruction");
    await expect(instruction).toBeVisible();

    const form = await page.locator(".schedules-form").evaluate((el) => el.getBoundingClientRect());
    const box = await instruction.evaluate((el) => el.getBoundingClientRect());
    // The input spans the form instead of hugging its right edge.
    expect(box.width).toBeGreaterThan(form.width * 0.8);

    // A real schedule, through the real form — a long instruction like the founder's.
    const long = "주간 통합 리포트를 딜리버러블로 발행해줘. ".repeat(20);
    await instruction.fill(long);
    await page.locator(".schedules-form__cron").fill("30 0 * * 1");
    await page.locator(".schedules-form__submit").click();
    const text = page.locator(".schedule-row__text, .schedule-row__title").first();
    await expect(text).toBeVisible();
    await shot(page, "schedules");

    const rowText = page.locator(".schedule-row__title").first();
    const h = await rowText.evaluate((el) => el.getBoundingClientRect().height);
    const line = await rowText.evaluate(
      (el) => Number.parseFloat(getComputedStyle(el).lineHeight) || 20,
    );
    expect(h).toBeLessThanOrEqual(line * 3.5);

    for (const hint of await page.locator(".schedules-form p").all()) {
      expect(await hint.evaluate((el) => getComputedStyle(el).textAlign)).not.toBe("right");
    }
  });
});
