import { DEFAULT_LOCALE, resolveLocale, resolveLocaleFromHeader } from "@/lib/i18n/config";
/**
 * Locale resolution (non-routed i18n). The active locale lives in the
 * `bsvibe.locale` cookie; the General → Language control writes it. On a FIRST
 * visit (no cookie) we auto-detect from the browser's `Accept-Language` header,
 * picking a SUPPORTED locale (en / ko) and otherwise falling back to English.
 * The explicit cookie choice always wins over the header.
 */
import { describe, expect, it } from "vitest";

describe("resolveLocale (cookie)", () => {
  it("narrows a supported value and falls back to the default otherwise", () => {
    expect(resolveLocale("ko")).toBe("ko");
    expect(resolveLocale("en")).toBe("en");
    expect(resolveLocale("fr")).toBe(DEFAULT_LOCALE);
    expect(resolveLocale(undefined)).toBe(DEFAULT_LOCALE);
    expect(resolveLocale(null)).toBe(DEFAULT_LOCALE);
  });
});

describe("resolveLocaleFromHeader (Accept-Language auto-detect)", () => {
  it("picks Korean when the browser prefers ko", () => {
    expect(resolveLocaleFromHeader("ko-KR,ko;q=0.9,en;q=0.8")).toBe("ko");
  });

  it("honors quality-ordered preference (en before ko)", () => {
    expect(resolveLocaleFromHeader("en-US,en;q=0.9,ko;q=0.5")).toBe("en");
  });

  it("falls back to English for an unsupported language", () => {
    expect(resolveLocaleFromHeader("fr-FR,fr;q=0.9")).toBe(DEFAULT_LOCALE);
  });

  it("falls back to English when the header is empty or missing", () => {
    expect(resolveLocaleFromHeader("")).toBe(DEFAULT_LOCALE);
    expect(resolveLocaleFromHeader(null)).toBe(DEFAULT_LOCALE);
    expect(resolveLocaleFromHeader(undefined)).toBe(DEFAULT_LOCALE);
  });

  it("matches a base language even when only a region-tagged variant is sent", () => {
    expect(resolveLocaleFromHeader("ko-KR")).toBe("ko");
  });
});
