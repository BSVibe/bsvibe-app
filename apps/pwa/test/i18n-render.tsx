/**
 * Test render shim: every component render is wrapped in
 * `NextIntlClientProvider` with the `en` catalog so `useTranslations` resolves
 * under vitest (which renders components in isolation, with no Next server).
 *
 * This module re-exports everything from `@testing-library/react` and overrides
 * `render` to inject the provider. `vitest.config.ts` aliases
 * `@testing-library/react` to this file, so existing tests keep importing
 * `{ render }` from `@testing-library/react` unchanged — and because the `en`
 * catalog values are byte-identical to the strings they replaced, every
 * existing `getByText`/role-name assertion still matches.
 *
 * The real library is reached via the `@rtl-actual` alias (defined in
 * vitest.config.ts) to avoid recursing through this shim.
 */

import enMessages from "@/messages/en.json";
import * as RTL from "@rtl-actual";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement, ReactNode } from "react";

function I18nWrapper({ children }: { children: ReactNode }) {
  return (
    <NextIntlClientProvider locale="en" messages={enMessages}>
      {children}
    </NextIntlClientProvider>
  );
}

type RenderOptions = Parameters<typeof RTL.render>[1];

function render(ui: ReactElement, options?: RenderOptions): ReturnType<typeof RTL.render> {
  const Wrapper = options?.wrapper;
  const Combined = Wrapper
    ? ({ children }: { children: ReactNode }) => (
        <I18nWrapper>
          <Wrapper>{children}</Wrapper>
        </I18nWrapper>
      )
    : I18nWrapper;
  return RTL.render(ui, { ...options, wrapper: Combined });
}

export * from "@rtl-actual";
export { render };
