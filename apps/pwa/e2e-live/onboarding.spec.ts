import { expect, test } from "@playwright/test";
import { signIn } from "./helpers/live-login";

/**
 * The **first-run** half of `docs/e2e/pwa-onboarding-checklist.md`.
 *
 * The vitest suite renders `OnboardingChecklist` / `BriefContent` directly with
 * hand-made props, so it can show any combination it likes — including ones the
 * product never produces. This file judges the combinations a real founder can
 * actually reach, on a stack whose database starts EMPTY.
 *
 * ⚠️ STATEFUL, unlike `authenticated-shell.spec.ts`. "First run" is by
 * definition a state you leave and cannot re-enter, so:
 *   * the walk is `serial` and ordered — the step that creates a product is
 *     LAST, because it ends the first-run state for everything above it, and
 *   * it asserts its own precondition instead of trusting the stack to be
 *     fresh. A second run against the same volume must fail loudly rather than
 *     quietly judge a different workspace.
 *
 *   docker compose -p bsvibe-e2e-live down -v     # ← required between runs
 */

const GET_STARTED = "Get started";
const ALL_CAUGHT_UP = "All caught up. Nothing running right now.";

/** KO first: it needs the first-run state the last test deliberately ends. */
test.describe("온보딩 — 한국어 로케일", () => {
  test.use({ locale: "ko-KR" });

  test("첫 실행 안내가 해요체 한국어로 나온다 — 영어 크롬이 안 샌다", async ({ page }) => {
    await signIn(page);

    const checklist = page.getByRole("region", { name: "시작하기" });
    await expect(checklist).toBeVisible();
    await expect(checklist).toContainText("첫 결과물까지 3단계예요.");
    await expect(checklist).toContainText("첫 제품 만들기");
    await expect(checklist).toContainText("워커 연결하기");
    await expect(checklist).toContainText("첫 요청 보내기");

    // A KO surface that leaks the EN catalogue reads as broken even when every
    // key resolves — the failure mode #742 and the KO gate checks both guard.
    await expect(checklist).not.toContainText(GET_STARTED);
    await expect(checklist).not.toContainText("Connect a worker");
  });
});

test.describe("온보딩 — 첫 실행 워크스페이스", () => {
  test.describe.configure({ mode: "serial" });

  test("0 제품 · 0 워커 워크스페이스는 빈 화면이 아니라 3단계 안내를 준다", async ({ page }) => {
    await signIn(page);

    const checklist = page.getByRole("region", { name: GET_STARTED });
    await expect(
      checklist,
      "첫 실행 안내가 없다 — 스택이 이미 안 비어 있으면 `docker compose -p bsvibe-e2e-live down -v` 후 다시 돌려라",
    ).toBeVisible();

    await expect(checklist).toContainText("Create your first product");
    await expect(checklist).toContainText("Connect a worker");
    await expect(checklist).toContainText("Send your first request");

    // The blocker this screen exists for: a new founder used to land on an
    // "all caught up" page that is true and useless.
    //
    // ⚠️ The proposition is NOT "that line is absent" — measured 2026-09-03, the
    // Working section still renders its empty state below the guidance, and an
    // absence assertion here failed against a perfectly healthy first run. What
    // the founder must not get is a page where the empty state is ALL there is,
    // so the honest check is that the guidance leads the page.
    const caughtUp = page.getByText(ALL_CAUGHT_UP);
    const guidanceBox = await checklist.boundingBox();
    if (!guidanceBox) throw new Error("첫 실행 안내의 위치를 잴 수 없다 — 화면에 없다");
    const emptyStateBox = (await caughtUp.count()) ? await caughtUp.boundingBox() : null;
    if (emptyStateBox) {
      expect(
        guidanceBox.y,
        "첫 실행 안내가 빈 상태 문구 아래로 밀렸다 — 새 사용자가 먼저 보는 것이 '모두 처리됨' 이 된다",
      ).toBeLessThan(emptyStateBox.y);
    }
  });

  test("워커 단계가 워커 설정 화면으로 데려간다 — 관리형 워커를 약속하지 않고", async ({
    page,
  }) => {
    await signIn(page);

    const link = page.getByRole("link", { name: "Set up a worker" });
    await expect(link).toBeVisible();
    await link.click();

    // The proposition is the DESTINATION, not "some settings page": the step is
    // only finishable on the executor-worker surface, which lives on the Models
    // tab. `/settings` server-redirects to General — and an earlier
    // `/\/settings$/` assertion passed here only by racing that redirect, which
    // is how a link one tab short of its target read as correct.
    await expect(page).toHaveURL(/\/settings\/models$/);
    await expect(page.getByRole("region", { name: "Executor workers" })).toBeVisible();
  });

  test("제품이 없을 때 요청을 보내면 제품을 먼저 만들라고 말한다 — 일반 전송 오류가 아니라", async ({
    page,
  }) => {
    await signIn(page);

    await page.getByRole("button", { name: "Request" }).click();
    const panel = page.getByRole("dialog", { name: "Request" });
    await expect(panel).toBeVisible();

    await panel.getByRole("textbox").fill("첫 요청 — 온보딩 E2E");
    // The submit button carries the same `direct.label` as the FAB, so it is
    // only unambiguous scoped INSIDE the dialog.
    await panel.getByRole("button", { name: "Request" }).click();

    // The proposition is the *localized* hint, not merely "an error appeared":
    // a raw English backend detail on a localized surface is the defect the
    // vitest suite pins, and only this path proves the 400 actually maps here.
    await expect(panel.getByText("Create a product first, then send your request.")).toBeVisible({
      timeout: 30_000,
    });
  });

  // LAST — creating a product ends the first-run state for every test above.
  test("제품을 만들면 1단계가 ✓ 로 바뀐다", async ({ page }) => {
    await signIn(page);

    await page.getByRole("button", { name: "New product" }).click();
    const slug = `onboarding-e2e-${Date.now()}`;
    await page.getByLabel("Name", { exact: true }).fill("Onboarding E2E");
    await page.getByLabel("Slug", { exact: true }).fill(slug);
    await page.getByRole("button", { name: "Create product" }).click();

    await page.goto("/brief");

    // `docs/e2e/pwa-onboarding-checklist.md` claims the step checks off, which
    // requires the checklist to still be on screen once a product exists.
    const checklist = page.getByRole("region", { name: GET_STARTED });
    await expect(
      checklist,
      "제품을 만든 뒤 온보딩 안내가 사라졌다 — 워커가 아직 없는데 남은 두 단계의 안내도 함께 사라진다",
    ).toBeVisible({ timeout: 30_000 });

    const productStep = checklist.getByRole("listitem").first();
    await expect(productStep).toContainText("Create your first product");
    await expect(productStep).toHaveClass(/onboarding__step--done/);
  });
});
