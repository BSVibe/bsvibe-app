"use client";

import { useCallback, useEffect, useState } from "react";
import {
  DEFAULT_PREFERENCES,
  type DisplayPreferences,
  getPreferences,
  setPreference,
} from "./preferences";

/**
 * Read + update display preferences from a client component. State starts at the
 * defaults (matching SSR / first hydration) and corrects to the stored values on
 * mount — deferring the localStorage read to `useEffect` avoids a hydration
 * mismatch on the rendered selects.
 */
export function usePreferences(): [
  DisplayPreferences,
  <K extends keyof DisplayPreferences>(key: K, value: DisplayPreferences[K]) => void,
] {
  const [prefs, setPrefs] = useState<DisplayPreferences>(DEFAULT_PREFERENCES);

  useEffect(() => {
    setPrefs(getPreferences());
  }, []);

  const update = useCallback(
    <K extends keyof DisplayPreferences>(key: K, value: DisplayPreferences[K]) => {
      setPreference(key, value);
      setPrefs((prev) => ({ ...prev, [key]: value }));
    },
    [],
  );

  return [prefs, update];
}
