import { expect, test } from "@playwright/test";
import { signIn } from "./helpers/live-login";

/** #961 — the product-check form on screen; a fresh stack has no products, so the product
 *  list is answered here. The schedule itself is created by the real backend. */
const SHOTS = process.env.BSVIBE_E2E_SHOTS_DIR;
const PRODUCT = "00000000-0000-4000-8000-000000000961";

test.use({ colorScheme: "dark", viewport: { width: 760, height: 1000 } });

test("제품 자율 점검을 폼으로 만든다", async ({ page }) => {
  await page.route("**/api/v1/products", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    await route.fulfill({
      json: [
        {
          id: PRODUCT,
          workspace_id: "ws",
          name: "BStockReport",
          slug: "bstockreport",
          repo_url: null,
          bootstrap_status: null,
          bootstrap_artifacts_count: null,
          bootstrap_error: null,
          bootstrap_progress: null,
          created_at: "2026-10-19T00:00:00Z",
          updated_at: "2026-10-19T00:00:00Z",
        },
      ],
    });
  });
  await signIn(page);
  await page.goto("/settings/schedules");
  await page.getByRole("radio", { name: /product check|제품 자율 점검/i }).check();
  await expect(page.getByRole("combobox", { name: /product|제품/i })).toHaveValue(PRODUCT);
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/tick-form.png`, fullPage: true });
  await page.getByRole("button", { name: /every day at 09:00|매일 09:00/i }).click();
  await page.getByRole("button", { name: /add schedule|예약 추가/i }).click();
  await expect(
    page.getByText(/product check · BStockReport|제품 자율 점검 · BStockReport/i),
  ).toBeVisible();
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/tick-list.png`, fullPage: true });
});
