/**
 * i18n wiring proofs.
 *
 *  - Rendering a converted component under the `ko` catalog shows Korean text
 *    (proves useTranslations resolves the active locale, not just `en`).
 *  - The `en` catalog values are byte-identical to the strings they replaced,
 *    which is what keeps the rest of the suite green (asserted indirectly by
 *    every other test passing, plus a direct equality check here).
 */

import BriefContent from "@/components/brief/BriefContent";
import type { BriefView } from "@/lib/api/types";
import enMessages from "@/messages/en.json";
import koMessages from "@/messages/ko.json";
// The shim wraps render in an `en` provider; reach the real render for the
// explicit `ko` provider used below.
import { render, screen } from "@rtl-actual";
import { NextIntlClientProvider } from "next-intl";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

const EMPTY_BRIEF: BriefView = { working: [], needsYou: [], stream: [], placeholder: true };

function ko(children: ReactNode) {
  return (
    <NextIntlClientProvider locale="ko" messages={koMessages}>
      {children}
    </NextIntlClientProvider>
  );
}

function NavLabels() {
  const t = useTranslations("nav");
  return (
    <ul>
      <li>{t("brief")}</li>
      <li>{t("decisions")}</li>
      <li>{t("settings")}</li>
    </ul>
  );
}

describe("i18n wiring", () => {
  it("renders Korean strings under the ko catalog", () => {
    render(
      <NextIntlClientProvider locale="ko" messages={koMessages}>
        <NavLabels />
      </NextIntlClientProvider>,
    );
    expect(screen.getByText("요약")).toBeInTheDocument();
    expect(screen.getByText("결정")).toBeInTheDocument();
    expect(screen.getByText("설정")).toBeInTheDocument();
  });

  it("renders the original English strings under the en catalog", () => {
    render(
      <NextIntlClientProvider locale="en" messages={enMessages}>
        <NavLabels />
      </NextIntlClientProvider>,
    );
    expect(screen.getByText("Brief")).toBeInTheDocument();
    expect(screen.getByText("Decisions")).toBeInTheDocument();
    expect(screen.getByText("Settings")).toBeInTheDocument();
  });

  it("renders a real surface (Brief) in Korean under the ko catalog", () => {
    render(ko(<BriefContent view={EMPTY_BRIEF} />));
    // Heading + the calm empty state, both from the brief catalog.
    expect(screen.getByText("요약")).toBeInTheDocument();
    expect(screen.getByText("지금은 확인할 것이 없어요.")).toBeInTheDocument();
  });

  it("en and ko catalogs share the exact same key shape", () => {
    const keyPaths = (obj: Record<string, unknown>, prefix = ""): string[] =>
      Object.entries(obj).flatMap(([k, v]) =>
        v && typeof v === "object"
          ? keyPaths(v as Record<string, unknown>, `${prefix}${k}.`)
          : [`${prefix}${k}`],
      );
    expect(keyPaths(koMessages).sort()).toEqual(keyPaths(enMessages).sort());
  });
});
