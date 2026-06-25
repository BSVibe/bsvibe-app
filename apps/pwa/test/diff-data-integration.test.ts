/**
 * Real integration (NOT mocked) — proves @git-diff-view/react actually parses
 * the hunk shapes we feed it: a full captured `git diff` section
 * (splitUnifiedDiffByFile) and a synthesized all-additions hunk
 * (synthesizeAdditionHunk). The component tests mock the library for speed; this
 * guards the format contract between our adapter and the library so a mismatch
 * fails here instead of silently rendering nothing in production.
 */

import { splitUnifiedDiffByFile, synthesizeAdditionHunk } from "@/lib/diff/diffData";
import { DiffFile } from "@git-diff-view/react";
import { describe, expect, it } from "vitest";

const MODIFY = `diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a + b
+    return a + b + 0
`;

function build(fileName: string, hunk: string): DiffFile {
  const file = DiffFile.createInstance({
    oldFile: { fileName },
    newFile: { fileName },
    hunks: [hunk],
  });
  file.initRaw();
  file.buildUnifiedDiffLines();
  return file;
}

describe("git-diff-view parses our adapter output", () => {
  it("parses a captured diff section into the right add/del counts", () => {
    const section = splitUnifiedDiffByFile(MODIFY).get("calc.py");
    expect(section).toBeDefined();
    if (!section) return;
    const file = build("calc.py", section);
    expect(file.additionLength).toBe(1);
    expect(file.deletionLength).toBe(1);
  });

  it("parses a synthesized additions hunk as all-additions", () => {
    const hunk = synthesizeAdditionHunk("notes.txt", "line one\nline two\nline three\n");
    const file = build("notes.txt", hunk);
    expect(file.additionLength).toBe(3);
    expect(file.deletionLength).toBe(0);
  });
});
