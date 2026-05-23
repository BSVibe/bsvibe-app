/**
 * Theme controller — dark mode for the PWA.
 *
 * A *preference* is what the founder chooses: "light" | "dark" | "system".
 * A *resolved theme* is the concrete look applied to the document: "light" |
 * "dark". For "system" we follow the OS via
 * `matchMedia('(prefers-color-scheme: dark)')`, live.
 *
 * The applied theme is expressed as `document.documentElement.dataset.theme`
 * (i.e. `<html data-theme="dark">`), which `globals.css` keys its dark token set
 * off of (`:root[data-theme="dark"]`). Light is the default `:root`, so
 * `data-theme="light"` and the absence of the attribute look identical — we set
 * it explicitly for clarity and so the segmented control reflects state.
 *
 * Flash-of-wrong-theme is prevented by an inline pre-hydration script (see
 * `themeBootScript`) injected in the root layout: it sets `data-theme` from
 * localStorage / matchMedia before first paint, before React hydrates.
 */

export type ThemePreference = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

export const THEME_STORAGE_KEY = "bsvibe.theme";

const DARK_QUERY = "(prefers-color-scheme: dark)";

function isPreference(value: unknown): value is ThemePreference {
  return value === "light" || value === "dark" || value === "system";
}

/** The persisted preference, defaulting to "system" when unset or corrupt. */
export function getThemePreference(): ThemePreference {
  if (typeof window === "undefined") return "system";
  const raw = window.localStorage.getItem(THEME_STORAGE_KEY);
  return isPreference(raw) ? raw : "system";
}

/** Persist a preference. (Applying it is `applyThemePreference`.) */
export function setThemePreference(pref: ThemePreference): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(THEME_STORAGE_KEY, pref);
}

/** True when the OS currently prefers a dark color scheme. */
function osPrefersDark(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return false;
  }
  return window.matchMedia(DARK_QUERY).matches;
}

/** Resolve a preference to a concrete theme. "system" follows the OS. */
export function resolveTheme(pref: ThemePreference): ResolvedTheme {
  if (pref === "system") return osPrefersDark() ? "dark" : "light";
  return pref;
}

/** Write the resolved theme onto `<html data-theme>`. */
function setDocumentTheme(theme: ResolvedTheme): void {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.theme = theme;
}

/**
 * Apply a preference now and, for "system", keep tracking the OS live.
 *
 * Returns a cleanup that detaches the OS listener (a no-op for explicit
 * light/dark, which don't subscribe). Callers that re-apply on a preference
 * change should invoke the previous cleanup first.
 */
export function applyThemePreference(pref: ThemePreference): () => void {
  setDocumentTheme(resolveTheme(pref));

  if (pref !== "system") return () => {};
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return () => {};
  }

  const mql = window.matchMedia(DARK_QUERY);
  const onChange = (e: MediaQueryListEvent) => {
    setDocumentTheme(e.matches ? "dark" : "light");
  };
  mql.addEventListener("change", onChange);
  return () => mql.removeEventListener("change", onChange);
}

/**
 * The inline script (as a string) injected into the root layout `<head>` so the
 * correct `data-theme` is set BEFORE first paint — no flash of the wrong theme
 * on load. Mirrors the resolution logic above without importing this module
 * (it runs before any bundle). Wrapped in try/catch so a storage exception
 * (private mode etc.) never blocks render.
 */
export const themeBootScript = `(function(){try{var k=${JSON.stringify(
  THEME_STORAGE_KEY,
)};var p=localStorage.getItem(k);if(p!=="light"&&p!=="dark"&&p!=="system"){p="system";}var dark=p==="dark"||(p==="system"&&window.matchMedia&&window.matchMedia(${JSON.stringify(
  DARK_QUERY,
)}).matches);document.documentElement.dataset.theme=dark?"dark":"light";}catch(e){}})();`;
