/** Caller-label resolution — known callers get a localized label, skill callers
 *  show their bare name, unknown ids fall back to the raw id. */

import { callerDisplay, callerLabelKey, skillCallerName } from "@/lib/routing-caller-labels";
import { describe, expect, it } from "vitest";

describe("routing caller labels", () => {
  it("maps known callers to a label key", () => {
    expect(callerLabelKey("workflow.agent_loop.plan")).toBe("plan");
    expect(callerLabelKey("knowledge.ingest")).toBe("ingest");
    expect(callerLabelKey("chat.completions")).toBe("chat");
    expect(callerLabelKey("nope")).toBeNull();
  });

  it("extracts a skill caller's bare name", () => {
    expect(skillCallerName("skill.widget-builder")).toBe("widget-builder");
    expect(skillCallerName("workflow.judge")).toBeNull();
  });

  it("callerDisplay localizes known callers via the translator", () => {
    const t = (key: string) => (key === "callerLabels.plan" ? "설계·계획" : key);
    expect(callerDisplay("workflow.agent_loop.plan", t)).toBe("설계·계획");
  });

  it("callerDisplay falls back to the skill name then the raw id", () => {
    const t = (key: string) => key;
    expect(callerDisplay("skill.my-skill", t)).toBe("my-skill");
    expect(callerDisplay("some.unknown.caller", t)).toBe("some.unknown.caller");
    expect(callerDisplay(null, t)).toBe("");
  });
});
