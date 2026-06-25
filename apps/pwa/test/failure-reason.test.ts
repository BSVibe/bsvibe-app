/**
 * L11 — humanizeFailureReason: a pure helper that turns the RAW internal run
 * failure reason (which can carry UUID-laden, engineer-facing text like
 * "loop crashed: executor chat task 61483a05-… failed: exit 1") into a calm,
 * plain-language sentence the founder can actually read. The mapping is
 * pattern-based (case-insensitive includes/regex), i18n-driven (the helper
 * takes a `t` so the workspace language wins), and never surfaces a raw
 * UUID/hash as the primary text — an unknown reason degrades to a generic line.
 */

import { humanizeFailureReason } from "@/lib/text/failure-reason";
import { describe, expect, it } from "vitest";

/** A tiny stand-in for next-intl's `t`: maps `run.failureHuman.*` keys to a
 *  stable marker so the test asserts WHICH bucket was chosen, independent of the
 *  real copy. */
const KEYS: Record<string, string> = {
  "failureHuman.executorCrash": "EXECUTOR_CRASH",
  "failureHuman.loopCrashed": "LOOP_CRASHED",
  "failureHuman.systemError": "SYSTEM_ERROR",
  "failureHuman.timeout": "TIMEOUT",
  "failureHuman.sandbox": "SANDBOX",
  "failureHuman.cancelled": "CANCELLED",
  "failureHuman.generic": "GENERIC",
};
const t = ((key: string) => KEYS[key] ?? key) as unknown as Parameters<
  typeof humanizeFailureReason
>[1];

describe("humanizeFailureReason", () => {
  it("maps an 'executor chat task … exit N' reason to the executor-crash line", () => {
    expect(
      humanizeFailureReason(
        "loop crashed: executor chat task 61483a05-1b2c-4d5e-8f90-abcdef123456 failed: exit 1",
        t,
      ),
    ).toBe("EXECUTOR_CRASH");
  });

  it("matches 'executor chat task' regardless of exit code value", () => {
    expect(humanizeFailureReason("executor chat task abc failed: exit 137", t)).toBe(
      "EXECUTOR_CRASH",
    );
  });

  it("maps a generic 'loop crashed' (no executor signature) to the loop-crashed line", () => {
    expect(humanizeFailureReason("loop crashed unexpectedly", t)).toBe("LOOP_CRASHED");
  });

  it("maps 'system error' to the system-error line", () => {
    expect(humanizeFailureReason("agent loop system error", t)).toBe("SYSTEM_ERROR");
  });

  it("maps a timeout reason to the timeout line (timed out)", () => {
    expect(humanizeFailureReason("the run timed out after 600s", t)).toBe("TIMEOUT");
  });

  it("maps a timeout reason to the timeout line (timeout)", () => {
    expect(humanizeFailureReason("hit hard timeout", t)).toBe("TIMEOUT");
  });

  it("maps a sandbox failure to the sandbox line", () => {
    expect(humanizeFailureReason("the work sandbox could not start", t)).toBe("SANDBOX");
  });

  it("maps a founder cancellation to the cancelled line (cancelled)", () => {
    expect(humanizeFailureReason("founder cancelled the run", t)).toBe("CANCELLED");
  });

  it("maps a founder discard to the cancelled line (discard)", () => {
    expect(humanizeFailureReason("founder discard requested", t)).toBe("CANCELLED");
  });

  it("is case-insensitive", () => {
    expect(humanizeFailureReason("AGENT LOOP SYSTEM ERROR", t)).toBe("SYSTEM_ERROR");
    expect(humanizeFailureReason("EXECUTOR CHAT TASK x failed: EXIT 1", t)).toBe("EXECUTOR_CRASH");
  });

  it("falls back to the generic line for an unknown reason", () => {
    expect(humanizeFailureReason("some brand new failure we never mapped", t)).toBe("GENERIC");
  });

  it("falls back to the generic line for an empty / whitespace reason", () => {
    expect(humanizeFailureReason("", t)).toBe("GENERIC");
    expect(humanizeFailureReason("   ", t)).toBe("GENERIC");
  });

  it("never surfaces a raw UUID as the primary text (even on the generic fallback)", () => {
    const out = humanizeFailureReason(
      "weird thing 61483a05-1b2c-4d5e-8f90-abcdef123456 happened",
      t,
    );
    // The known buckets win when they match; here it's generic — and the UUID
    // must NOT leak into whatever the helper returns.
    expect(out).toBe("GENERIC");
    expect(out).not.toMatch(/[0-9a-f]{8}-[0-9a-f]{4}/i);
  });

  it("priority: an executor-crash signature wins over the bare loop-crashed match", () => {
    // The string contains BOTH "loop crashed" and "executor chat task … exit";
    // the more specific executor bucket must win.
    expect(humanizeFailureReason("loop crashed: executor chat task z failed: exit 2", t)).toBe(
      "EXECUTOR_CRASH",
    );
  });
});
