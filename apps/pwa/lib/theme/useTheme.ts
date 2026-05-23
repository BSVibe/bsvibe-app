"use client";

import { useCallback, useEffect, useState } from "react";
import {
  type ThemePreference,
  applyThemePreference,
  getThemePreference,
  setThemePreference,
} from "./theme";

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
