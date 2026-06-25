/**
 * Diff-data helpers (Lift 3a) — split a unified patch per file, synthesize an
 * all-additions hunk for a no-before file, and map filenames to highlight langs.
 */

import {
  langFromFileName,
  splitUnifiedDiffByFile,
  synthesizeAdditionHunk,
} from "@/lib/diff/diffData";
import { describe, expect, it } from "vitest";

const MODIFY = `diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,1 +1,1 @@
-old
+new
`;

const NEW_FILE = `diff --git a/src/foo.py b/src/foo.py
new file mode 100644
--- /dev/null
+++ b/src/foo.py
@@ -0,0 +1,1 @@
+def foo(): ...
`;

describe("splitUnifiedDiffByFile", () => {
  it("keys each file's full diff section by its new path", () => {
    const map = splitUnifiedDiffByFile(MODIFY + NEW_FILE);
    expect([...map.keys()].sort()).toEqual(["calc.py", "src/foo.py"]);
    expect(map.get("calc.py")).toContain("diff --git a/calc.py b/calc.py");
    expect(map.get("calc.py")).toContain("-old");
    expect(map.get("calc.py")).toContain("+new");
  });

  it("keys a deletion by its old path", () => {
    const deleted = `diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1,1 +0,0 @@
-gone
`;
    const map = splitUnifiedDiffByFile(deleted);
    expect([...map.keys()]).toEqual(["old.py"]);
  });

  it("returns an empty map for an empty/blank patch", () => {
    expect(splitUnifiedDiffByFile("").size).toBe(0);
    expect(splitUnifiedDiffByFile("   \n").size).toBe(0);
  });
});

describe("synthesizeAdditionHunk", () => {
  it("wraps content as a full new-file additions section", () => {
    const hunk = synthesizeAdditionHunk("notes.txt", "a\nb\nc\n");
    // The git-diff-view headers are required for the additions to render.
    expect(hunk).toContain("diff --git a/notes.txt b/notes.txt");
    expect(hunk).toContain("--- /dev/null");
    expect(hunk).toContain("+++ b/notes.txt");
    // The trailing newline does not inflate the hunk count.
    expect(hunk).toContain("@@ -0,0 +1,3 @@");
    expect(hunk).toContain("+a");
    expect(hunk).toContain("+c");
  });

  it("keeps interior blank lines in the count", () => {
    const hunk = synthesizeAdditionHunk("notes.txt", "a\n\nb");
    expect(hunk).toContain("@@ -0,0 +1,3 @@");
  });
});

describe("langFromFileName", () => {
  it("maps known extensions", () => {
    expect(langFromFileName("calc.py")).toBe("python");
    expect(langFromFileName("src/app.ts")).toBe("typescript");
    expect(langFromFileName("README.md")).toBe("markdown");
  });

  it("is undefined for unknown / extensionless files", () => {
    expect(langFromFileName("Makefile")).toBeUndefined();
    expect(langFromFileName("notes.xyz")).toBeUndefined();
  });
});
