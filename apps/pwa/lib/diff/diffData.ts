/**
 * Diff data helpers (Lift 3a) — adapt our captured `git diff` (Lift 2a) and raw
 * file content into the shape `@git-diff-view/react` consumes (`data.hunks`).
 *
 * Pure + dependency-free + tolerant: malformed input degrades to empty/no-op
 * rather than throwing, so the viewer never crashes on an odd patch.
 */

/** Strip a leading `a/` or `b/` prefix from a `---`/`+++` diff path. */
function stripPrefix(raw: string): string {
  return raw.startsWith("a/") || raw.startsWith("b/") ? raw.slice(2) : raw;
}

/**
 * Split a unified `git diff` patch into a `path → per-file diff text` map. The
 * value is the full `diff --git …` section for that file (headers + hunks),
 * which `@git-diff-view/react` parses directly as a `hunks` entry. Keyed by the
 * new (`+++ b/`) path, or the old (`--- a/`) path for a deletion.
 */
export function splitUnifiedDiffByFile(unified: string): Map<string, string> {
  const map = new Map<string, string>();
  if (!unified.trim()) return map;

  let buf: string[] | null = null;
  let oldPath: string | null = null;
  let newPath: string | null = null;

  const flush = () => {
    if (buf === null) return;
    const path =
      newPath && newPath !== "/dev/null"
        ? stripPrefix(newPath)
        : oldPath && oldPath !== "/dev/null"
          ? stripPrefix(oldPath)
          : null;
    if (path !== null) map.set(path, buf.join("\n"));
  };

  for (const line of unified.split("\n")) {
    if (line.startsWith("diff --git ")) {
      flush();
      buf = [line];
      oldPath = null;
      newPath = null;
      continue;
    }
    if (buf === null) continue;
    buf.push(line);
    if (line.startsWith("--- ")) oldPath = line.slice(4).trim();
    else if (line.startsWith("+++ ")) newPath = line.slice(4).trim();
  }
  flush();
  return map;
}

/**
 * Build a full new-file unified-diff section that renders the whole of `content`
 * as additions — for a freshly produced file with no captured "before". The
 * `diff --git` / `--- /dev/null` / `+++ b/<file>` headers are REQUIRED:
 * @git-diff-view parses a bare `@@` hunk as zero changes, so the headers are
 * what make the additions actually render. A single trailing newline is dropped
 * so it doesn't render a phantom blank addition.
 */
export function synthesizeAdditionHunk(fileName: string, content: string): string {
  const lines = content.split("\n");
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  const body = lines.map((line) => `+${line}`).join("\n");
  return [
    `diff --git a/${fileName} b/${fileName}`,
    "new file mode 100644",
    "--- /dev/null",
    `+++ b/${fileName}`,
    `@@ -0,0 +1,${lines.length} @@`,
    body,
  ].join("\n");
}

/** Map a filename's extension to a highlight.js language id, or `undefined`
 *  (plain text) for unknown / extensionless files. Covers the languages a
 *  deliverable is likely to contain; the viewer falls back to plain text for
 *  the rest, so this list is an enhancement, never a gate. */
const _EXT_LANG: Record<string, string> = {
  ts: "typescript",
  tsx: "typescript",
  js: "javascript",
  jsx: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  py: "python",
  rb: "ruby",
  go: "go",
  rs: "rust",
  java: "java",
  kt: "kotlin",
  c: "c",
  h: "c",
  cpp: "cpp",
  cc: "cpp",
  cs: "csharp",
  php: "php",
  swift: "swift",
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  sql: "sql",
  json: "json",
  yaml: "yaml",
  yml: "yaml",
  toml: "toml",
  md: "markdown",
  markdown: "markdown",
  html: "xml",
  xml: "xml",
  css: "css",
  scss: "scss",
};

export function langFromFileName(fileName: string): string | undefined {
  const dot = fileName.lastIndexOf(".");
  if (dot < 0) return undefined;
  return _EXT_LANG[fileName.slice(dot + 1).toLowerCase()];
}
