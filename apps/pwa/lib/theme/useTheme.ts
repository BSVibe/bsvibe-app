"use client";

import { useCallback, useEffect, useState } from "react";
import {
  type ResolvedTheme,
  type ThemePreference,
  applyThemePreference,
  getThemePreference,
  setThemePreference,
} from "./theme";

/**
 * The RESOLVED theme currently applied to the document (`<html data-theme>`),
 * live. Reads the attribute on mount and follows changes via a MutationObserver
 * so a component re-renders when the founder flips light/dark (or the OS does,
 * under "system"). Defaults to "light" during SSR/first paint (the `:root`
 * default), correcting on mount — no hydration mismatch on a value not rendered
 * until the effect runs.
 */
export function useResolvedTheme(): ResolvedTheme {
  const [theme, setTheme] = useState<ResolvedTheme>("light");

  useEffect(() => {
    const read = (): ResolvedTheme =>
      document.documentElement.dataset.theme === "dark" ? "dark" : "light";
    setTheme(read());
    const observer = new MutationObserver(() => setTheme(read()));
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    return () => observer.disconnect();
  }, []);

  return theme;
}

/**
 * Read + set the theme preference from a client component.
 *
 * Returns the current preference and a setter. The setter persists the choice
 * and re-applies it to the document (tearing down any prior "system" OS
 * listener first, so we never stack subscriptions). On mount it (re-)applies the
 * stored preference and, for "system", keeps following the OS for as long as the
 * component is mounted.
 *
 * `getThemePreference()` reads localStorage, so initial state is "system" during
 * SSR/first hydration and corrects on mount — kept consistent by deferring the
 * read to `useEffect` rather than the initializer, avoiding a hydration
 * mismatch on the rendered control.
 */
export function useThemePreference(): [ThemePreference, (next: ThemePreference) => void] {
  const [pref, setPref] = useState<ThemePreference>("system");

  useEffect(() => {
    const stored = getThemePreference();
    setPref(stored);
    const cleanup = applyThemePreference(stored);
    return cleanup;
  }, []);

  const choose = useCallback((next: ThemePreference) => {
    setThemePreference(next);
    setPref(next);
    applyThemePreference(next);
  }, []);

  return [pref, choose];
}
