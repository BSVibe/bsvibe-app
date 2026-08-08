/**
 * Every message in every locale must parse under next-intl's ICU parser.
 *
 * next-intl treats `<name>` as a rich-text tag. A message that mentions a
 * placeholder in prose — `claude mcp add bsvibe <url>` — therefore fails to
 * parse with `INVALID_MESSAGE: UNCLOSED_TAG`, and the UI renders the error
 * instead of the sentence. Nothing crashes and no test fails, so the copy is
 * simply missing until someone looks at that screen.
 *
 * This walks every leaf key rather than pinning the one we found: the trap is
 * a property of writing angle brackets in copy, and the next one will be
 * written by someone who never saw this bug.
 *
 * Only parse failures are asserted. A message with required values (`{days}`)
 * raises a different code when called without them, which is not a defect.
 */

import { createTranslator } from "next-intl";
import { describe, expect, it } from "vitest";
import en from "../messages/en.json";
import ko from "../messages/ko.json";

type Messages = { [key: string]: string | Messages };

function leafKeys(node: Messages, prefix = ""): string[] {
  return Object.entries(node).flatMap(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    return typeof value === "string" ? [path] : leafKeys(value, path);
  });
}

function unparseableMessages(locale: string, messages: Messages): string[] {
  const broken: string[] = [];
  let current = "";
  const t = createTranslator({
    locale,
    messages: messages as never,
    onError(error) {
      // MISSING_FORMAT_VALUE / FORMATTING_ERROR just mean we called a
      // parameterised message with no values — not a defect in the message.
      if (error.code === "INVALID_MESSAGE") {
        broken.push(`${current} — ${error.message}`);
      }
    },
  });

  for (const key of leafKeys(messages)) {
    current = key;
    try {
      t(key as never);
    } catch {
      // A throw here is a formatting failure, already covered by onError.
    }
  }
  return broken;
}

describe("message catalogues parse", () => {
  it.each([
    ["en", en as Messages],
    ["ko", ko as Messages],
  ])("%s has no unparseable messages", (locale, messages) => {
    expect(unparseableMessages(locale, messages)).toEqual([]);
  });

  it("en and ko expose exactly the same keys", () => {
    const enKeys = leafKeys(en as Messages).sort();
    const koKeys = leafKeys(ko as Messages).sort();
    expect(koKeys.filter((k) => !enKeys.includes(k))).toEqual([]);
    expect(enKeys.filter((k) => !koKeys.includes(k))).toEqual([]);
  });
});
