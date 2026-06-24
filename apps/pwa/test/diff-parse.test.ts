/**
 * Unified-diff parser (Lift 2b) — turns a `git diff` patch into a per-file map
 * of typed lines (add / del / context) with old+new line numbers, so the
 * delivery report can render GitHub-style red/green. Pure, dependency-free.
 */

import { parseUnifiedDiff } from "@/lib/diff/parseUnifiedDiff";
import { describe, expect, it } from "vitest";

const MODIFY = `diff --git a/calc.py b/calc.py
index e69de29..4b825dc 100644
--- a/calc.py
+++ b/calc.py
@@ -1,3 +1,3 @@
 def add(a, b):
-    return a + b
+    return a + b + 0
`;

const NEW_FILE = `diff --git a/src/foo.py b/src/foo.py
new file mode 100644
index 0000000..b1e6722
--- /dev/null
+++ b/src/foo.py
@@ -0,0 +1,2 @@
+def foo():
+    return 1
`;

const TWO_FILES = MODIFY + NEW_FILE;

describe("parseUnifiedDiff", () => {
  it("keys file diffs by their new path", () => {
    const map = parseUnifiedDiff(TWO_FILES);
    expect([...map.keys()].sort()).toEqual(["calc.py", "src/foo.py"]);
  });

  it("marks removed and added lines with old/new line numbers", () => {
    const file = parseUnifiedDiff(MODIFY).get("calc.py");
    expect(file).toBeDefined();
    if (!file) return;
    // context, del, add
    expect(file.lines.map((l) => l.kind)).toEqual(["context", "del", "add"]);

    const [ctx, del, add] = file.lines;
    expect(ctx).toMatchObject({
      kind: "context",
      oldNumber: 1,
      newNumber: 1,
      text: "def add(a, b):",
    });
    expect(del).toMatchObject({
      kind: "del",
      oldNumber: 2,
      newNumber: null,
      text: "    return a + b",
    });
    expect(add).toMatchObject({
      kind: "add",
      oldNumber: null,
      newNumber: 2,
      text: "    return a + b + 0",
    });

    expect(file.additions).toBe(1);
    expect(file.deletions).toBe(1);
    expect(file.isNew).toBe(false);
  });

  it("flags a new file and renders every line as an addition", () => {
    const file = parseUnifiedDiff(NEW_FILE).get("src/foo.py");
    expect(file).toBeDefined();
    if (!file) return;
    expect(file.isNew).toBe(true);
    expect(file.lines.every((l) => l.kind === "add")).toBe(true);
    expect(file.lines.map((l) => l.newNumber)).toEqual([1, 2]);
    expect(file.additions).toBe(2);
    expect(file.deletions).toBe(0);
  });

  it("flags a deleted file via its old path", () => {
    const deleted = `diff --git a/old.py b/old.py
deleted file mode 100644
index b1e6722..0000000
--- a/old.py
+++ /dev/null
@@ -1,1 +0,0 @@
-gone = True
`;
    const file = parseUnifiedDiff(deleted).get("old.py");
    expect(file).toBeDefined();
    if (!file) return;
    expect(file.isDeleted).toBe(true);
    expect(file.lines.map((l) => l.kind)).toEqual(["del"]);
  });

  it("handles multiple hunks with correct running line numbers", () => {
    const multi = `diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,2 +1,2 @@
 a = 1
-b = 2
+b = 3
@@ -10,2 +10,3 @@
 j = 10
+k = 11
 l = 12
`;
    const file = parseUnifiedDiff(multi).get("m.py");
    expect(file).toBeDefined();
    if (!file) return;
    const added = file.lines.find((l) => l.text === "k = 11");
    expect(added).toMatchObject({ kind: "add", newNumber: 11 });
    const ctxAfter = file.lines.find((l) => l.text === "l = 12");
    expect(ctxAfter).toMatchObject({ kind: "context", oldNumber: 11, newNumber: 12 });
  });

  it("ignores the no-newline marker and tolerates an empty/blank patch", () => {
    expect(parseUnifiedDiff("").size).toBe(0);
    expect(parseUnifiedDiff("   \n").size).toBe(0);
    const noNewline = `diff --git a/x b/x
--- a/x
+++ b/x
@@ -1 +1 @@
-old
\\ No newline at end of file
+new
\\ No newline at end of file
`;
    const file = parseUnifiedDiff(noNewline).get("x");
    expect(file?.lines.map((l) => l.kind)).toEqual(["del", "add"]);
  });
});
