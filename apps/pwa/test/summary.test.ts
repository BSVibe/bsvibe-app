/**
 * conciseSummary (lib/text/summary.ts) — the shared first-sentence condenser
 * used by the Brief rows, the product "Shipped" list, and the Delivery Report
 * title so a shipped result reads calmly everywhere.
 */

import { conciseSummary } from "@/lib/text/summary";
import { describe, expect, it } from "vitest";

describe("conciseSummary", () => {
  it("returns the fallback for an empty / null summary", () => {
    expect(conciseSummary(null, "Shipped deliverable")).toBe("Shipped deliverable");
    expect(conciseSummary("   \n  ", "Shipped deliverable")).toBe("Shipped deliverable");
  });

  it("keeps a short single-line summary as-is", () => {
    expect(conciseSummary("Add getRelatedPosts", "x")).toBe("Add getRelatedPosts");
  });

  it("takes the first line of a multi-line summary", () => {
    expect(conciseSummary("Publish the launch page\nwith images", "x")).toBe(
      "Publish the launch page",
    );
  });

  it("condenses a long blob to its first sentence (not splitting fibonacci.py)", () => {
    const blob =
      "The fibonacci.py file has been successfully created with a function that " +
      "returns the nth Fibonacci number. The implementation handles n=0 and n=1.";
    expect(conciseSummary(blob, "x")).toBe(
      "The fibonacci.py file has been successfully created with a function that returns the nth Fibonacci number.",
    );
  });

  it("hard-caps an over-long single sentence with an ellipsis", () => {
    const huge = `${"word ".repeat(60).trim()} end`;
    const out = conciseSummary(huge, "x");
    expect(out.endsWith("…")).toBe(true);
    expect(out.length).toBeLessThanOrEqual(141);
  });
});
