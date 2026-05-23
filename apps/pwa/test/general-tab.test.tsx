/**
 * General tab — the must-work surface of this lift.
 *
 *  - Theme: a Light / System / Dark segmented control wired to the theme
 *    controller. Choosing one persists the preference (bsvibe.theme) AND applies
 *    `data-theme` to <html>.
 *  - Language / Time zone / Date format: local-only preferences that persist
 *    their selected value to localStorage.
 *  - Workspace ID: display-only (the session's personal account id, or a calm
 *    fallback). No backend writes.
 */

import GeneralTab from "@/components/settings/GeneralTab";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { LOCALE_COOKIE } from "@/lib/i18n/config";
import { PREF_STORAGE_KEY } from "@/lib/preferences/preferences";
import { THEME_STORAGE_KEY } from "@/lib/theme/theme";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// GeneralTab refreshes the route after switching the locale; stub the router.
const refresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh, replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
}));

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
  personalAccountId: "acct-abc-123",
};

beforeEach(() => {
  window.localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
  document.cookie = `${LOCALE_COOKIE}=; path=/; max-age=0`;
  refresh.mockClear();
  clearSession();
  setSession(SESSION);
  // jsdom lacks matchMedia — provide a light-preferring stub.
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      matches: false,
      media: "(prefers-color-scheme: dark)",
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => true,
      onchange: null,
    })),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("General tab — theme control", () => {
  it("renders Light / System / Dark options", () => {
    render(<GeneralTab />);
    expect(screen.getByRole("radio", { name: /light/i })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /system/i })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /dark/i })).toBeInTheDocument();
  });

  it("choosing Dark persists the preference and applies data-theme=dark", async () => {
    const user = userEvent.setup();
    render(<GeneralTab />);

    await user.click(screen.getByRole("radio", { name: /dark/i }));

    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("choosing Light persists the preference and applies data-theme=light", async () => {
    const user = userEvent.setup();
    render(<GeneralTab />);

    await user.click(screen.getByRole("radio", { name: /dark/i }));
    await user.click(screen.getByRole("radio", { name: /light/i }));

    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
    expect(document.documentElement.dataset.theme).toBe("light");
  });
});

describe("General tab — display preferences", () => {
  it("persists the selected language", async () => {
    const user = userEvent.setup();
    render(<GeneralTab />);

    await user.selectOptions(screen.getByLabelText(/language/i), "ko");

    expect(JSON.parse(window.localStorage.getItem(PREF_STORAGE_KEY) ?? "{}").language).toBe("ko");
  });

  it("persists the selected time zone", async () => {
    const user = userEvent.setup();
    render(<GeneralTab />);

    await user.selectOptions(screen.getByLabelText(/time zone/i), "UTC");

    expect(JSON.parse(window.localStorage.getItem(PREF_STORAGE_KEY) ?? "{}").timezone).toBe("UTC");
  });

  it("persists the selected date format", async () => {
    const user = userEvent.setup();
    render(<GeneralTab />);

    await user.selectOptions(screen.getByLabelText(/date format/i), "us");

    expect(JSON.parse(window.localStorage.getItem(PREF_STORAGE_KEY) ?? "{}").dateFormat).toBe("us");
  });

  it("switching language sets the locale cookie and refreshes the route", async () => {
    const user = userEvent.setup();
    render(<GeneralTab />);

    await user.selectOptions(screen.getByLabelText(/language/i), "ko");

    expect(document.cookie).toContain(`${LOCALE_COOKIE}=ko`);
    expect(refresh).toHaveBeenCalled();
  });
});

describe("General tab — workspace identity", () => {
  it("shows the workspace id from the session (display-only)", () => {
    render(<GeneralTab />);
    expect(screen.getByText("acct-abc-123")).toBeInTheDocument();
  });

  it("does not render a danger zone in this lift", () => {
    render(<GeneralTab />);
    expect(screen.queryByText(/delete workspace/i)).not.toBeInTheDocument();
  });
});
