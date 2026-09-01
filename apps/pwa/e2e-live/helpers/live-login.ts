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
  await page.getByLabel(/email/i).fill(email);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: /continue$/i }).click();

  // `/login` replaces to `return_to` (default `/brief`) only after the POST
  // resolves, so this is the first point at which a session exists.
  await expect(page).toHaveURL(/\/brief$/, { timeout: 30_000 });
}
