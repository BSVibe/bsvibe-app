/**
 * Client display preferences (language / time zone / date format) — local-only,
 * persisted to localStorage. No backend sync yet (documented follow-up). These
 * assert the round-trip and the sane defaults.
 */

import {
  DATE_FORMAT_OPTIONS,
  LANGUAGE_OPTIONS,
  PREF_STORAGE_KEY,
  TIMEZONE_OPTIONS,
  getPreferences,
  setPreference,
} from "@/lib/preferences/preferences";
import { beforeEach, describe, expect, it } from "vitest";

describe("display preferences", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("provides sane defaults when nothing is stored", () => {
    const prefs = getPreferences();
    expect(prefs.language).toBe("en");
    expect(prefs.timezone).toBe("Asia/Seoul");
    expect(prefs.dateFormat).toBe("iso");
  });

  it("offers English and Korean languages", () => {
    const values = LANGUAGE_OPTIONS.map((o) => o.value);
    expect(values).toContain("en");
    expect(values).toContain("ko");
  });

  it("offers Asia/Seoul and UTC time zones", () => {
    const values = TIMEZONE_OPTIONS.map((o) => o.value);
    expect(values).toContain("Asia/Seoul");
    expect(values).toContain("UTC");
  });

  it("offers at least two date formats", () => {
    expect(DATE_FORMAT_OPTIONS.length).toBeGreaterThanOrEqual(2);
  });

  it("persists a single preference and reads it back", () => {
    setPreference("language", "ko");
    expect(getPreferences().language).toBe("ko");
    expect(JSON.parse(window.localStorage.getItem(PREF_STORAGE_KEY) ?? "{}").language).toBe("ko");
  });

  it("merges partial updates without dropping other fields", () => {
    setPreference("timezone", "UTC");
    setPreference("dateFormat", "us");
    const prefs = getPreferences();
    expect(prefs.timezone).toBe("UTC");
    expect(prefs.dateFormat).toBe("us");
    expect(prefs.language).toBe("en"); // untouched -> default
  });

  it("ignores a corrupt stored blob and falls back to defaults", () => {
    window.localStorage.setItem(PREF_STORAGE_KEY, "{not json");
    expect(getPreferences().language).toBe("en");
  });
});
