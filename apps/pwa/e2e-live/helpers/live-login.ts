import { type Page, expect } from "@playwright/test";
import { readLiveCredentials } from "../guard";

/**
 * Sign in the way a founder does: type into the real form and press the real
 * button.
 *
 * ⚠️ There is deliberately **no dev-login bypass and no token injection**. A
 * helper that seeds a session directly would make every test below pass while
 * the actual sign-in path was broken — which is the one path a user cannot
 * route around. The cost is a real round trip to GoTrue per test; that is the
 * price of the assertion meaning anything.
 */
export async function signIn(page: Page): Promise<void> {
  const { email, password } = readLiveCredentials(process.env);

  await page.goto("/login");
  // ⚠️ Selected by the form's own ids / submit type, NOT by label text. The
  // locale is decided by `Accept-Language` on a first visit, so an English
  // label selector silently made this helper English-only: a `locale: "ko-KR"`
  // test failed in `signIn` with a 30s timeout that read as "Korean onboarding
  // is broken" when the product was fine and the harness was not. These
  // selectors still type into the real form and press the real button — the
  // no-bypass property below is unchanged.
  await page.locator("#email").fill(email);
  await page.locator("#password").fill(password);
  await page.locator('form button[type="submit"]').click();

  // `/login` replaces to `return_to` (default `/brief`) only after the POST
  // resolves, so this is the first point at which a session exists.
  await expect(page).toHaveURL(/\/brief$/, { timeout: 30_000 });
}
