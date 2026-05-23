/**
 * Theme controller — resolves a persisted preference ("light" | "dark" |
 * "system") into a concrete applied theme on `document.documentElement`, follows
 * the OS via matchMedia for "system", and persists the preference to
 * localStorage under `bsvibe.theme`.
 *
 * These are the load-bearing guarantees of dark mode: the right preference must
 * resolve to the right `data-theme`, and the choice must round-trip storage.
 */

import {
  THEME_STORAGE_KEY,
  applyThemePreference,
  getThemePreference,
  resolveTheme,
  setThemePreference,
} from "@/lib/theme/theme";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/** Install a matchMedia mock whose `(prefers-color-scheme: dark)` query
 *  reports `dark`. Returns the listener registry so a test can flip it live. */
function mockMatchMedia(prefersDark: boolean) {
  const listeners = new Set<(e: MediaQueryListEvent) => void>();
  let matches = prefersDark;
  const mql = {
    get matches() {
      return matches;
    },
    media: "(prefers-color-scheme: dark)",
    addEventListener: (_: string, cb: (e: MediaQueryListEvent) => void) => {
      listeners.add(cb);
    },
    removeEventListener: (_: string, cb: (e: MediaQueryListEvent) => void) => {
      listeners.delete(cb);
    },
    // legacy API some libs touch — keep harmless
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => true,
    onchange: null,
  };
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => mql),
  );
  // expose a flipper
  return {
    setDark(next: boolean) {
      matches = next;
      for (const cb of listeners) cb({ matches: next } as MediaQueryListEvent);
    },
  };
}

describe("theme controller", () => {
  beforeEach(() => {
    window.localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("defaults the preference to 'system' when nothing is stored", () => {
    expect(getThemePreference()).toBe("system");
  });

  it("persists and reads back a preference under bsvibe.theme", () => {
    setThemePreference("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(getThemePreference()).toBe("dark");
  });

  it("ignores a corrupt stored value and falls back to 'system'", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "neon");
    expect(getThemePreference()).toBe("system");
  });

  it("resolves an explicit 'dark' preference to 'dark'", () => {
    mockMatchMedia(false); // OS says light — explicit pref must win
    expect(resolveTheme("dark")).toBe("dark");
  });

  it("resolves an explicit 'light' preference to 'light'", () => {
    mockMatchMedia(true); // OS says dark — explicit pref must win
    expect(resolveTheme("light")).toBe("light");
  });

  it("resolves 'system' by following matchMedia (dark)", () => {
    mockMatchMedia(true);
    expect(resolveTheme("system")).toBe("dark");
  });

  it("resolves 'system' by following matchMedia (light)", () => {
    mockMatchMedia(false);
    expect(resolveTheme("system")).toBe("light");
  });

  it("applies a 'dark' preference by setting data-theme='dark' on <html>", () => {
    mockMatchMedia(false);
    applyThemePreference("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("applies a 'light' preference by setting data-theme='light' on <html>", () => {
    mockMatchMedia(true);
    applyThemePreference("light");
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("applies 'system' by resolving the OS preference (dark)", () => {
    mockMatchMedia(true);
    applyThemePreference("system");
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("returns a cleanup that stops following the OS for 'system'", () => {
    const media = mockMatchMedia(false);
    const cleanup = applyThemePreference("system");
    expect(document.documentElement.dataset.theme).toBe("light");

    // Live OS flip -> applied theme tracks it while subscribed.
    media.setDark(true);
    expect(document.documentElement.dataset.theme).toBe("dark");

    cleanup();
    // After cleanup, further OS flips are ignored.
    media.setDark(false);
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("does not subscribe to the OS for an explicit preference", () => {
    const media = mockMatchMedia(false);
    applyThemePreference("dark");
    media.setDark(true);
    // Explicit dark stays dark regardless of OS changes.
    expect(document.documentElement.dataset.theme).toBe("dark");
  });
});
