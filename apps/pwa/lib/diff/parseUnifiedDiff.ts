/**
 * Unified-diff parser (Lift 2b). Turns a `git diff` patch (as captured at
 * verify-time and served by `GET /api/v1/deliverables/{id}/diff`) into a per-file
 * map of typed lines so the delivery report can render GitHub-style red/green.
 *
 * Pure + dependency-free + tolerant: a malformed/empty patch yields an empty map
 * rather than throwing — the viewer falls back to content-as-additions.
 */

export type DiffLineKind = "add" | "del" | "context";

export interface DiffLine {
  kind: DiffLineKind;
  /** Line number in the old file (null for an added line). */
  oldNumber: number | null;
  /** Line number in the new file (null for a removed line). */
  newNumber: number | null;
  /** The line content, without the leading +/-/space marker. */
  text: string;
}

export interface FileDiff {
  /** The file's path (the new `b/` path; the old `a/` path for a deletion). */
  path: string;
  isNew: boolean;
  isDeleted: boolean;
  isBinary: boolean;
  lines: DiffLine[];
  additions: number;
  deletions: number;
}

const HUNK_HEADER = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

/** Strip a leading `a/` or `b/` prefix from a `--- `/`+++ ` path. */
function stripPrefix(raw: string): string {
  if (raw.startsWith("a/") || raw.startsWith("b/")) return raw.slice(2);
  return raw;
}

/** Resolve a file section's canonical path: the new (`+++ b/`) path, or the old
 *  (`--- a/`) path when the new side is `/dev/null` (a deletion). */
function resolvePath(oldPath: string | null, newPath: string | null): string | null {
  if (newPath && newPath !== "/dev/null") return stripPrefix(newPath);
  if (oldPath && oldPath !== "/dev/null") return stripPrefix(oldPath);
  return null;
}

function parseSection(section: string[]): FileDiff | null {
  let oldPath: string | null = null;
  let newPath: string | null = null;
  let isNew = false;
  let isDeleted = false;
  let isBinary = false;
  const lines: DiffLine[] = [];
  let oldNo = 0;
  let newNo = 0;
  let additions = 0;
  let deletions = 0;
  let inHunk = false;

  for (const line of section) {
    if (line.startsWith("new file mode")) {
      isNew = true;
      continue;
    }
    if (line.startsWith("deleted file mode")) {
      isDeleted = true;
      continue;
    }
    if (line.startsWith("Binary files")) {
      isBinary = true;
      continue;
    }
    if (line.startsWith("--- ")) {
      oldPath = line.slice(4).trim();
      continue;
    }
    if (line.startsWith("+++ ")) {
      newPath = line.slice(4).trim();
      continue;
    }
    const hunk = HUNK_HEADER.exec(line);
    if (hunk) {
      oldNo = Number(hunk[1]);
      newNo = Number(hunk[2]);
      inHunk = true;
      continue;
    }
    if (!inHunk) continue;
    if (line.startsWith("\\")) continue; // "\ No newline at end of file"
    const marker = line[0];
    const text = line.slice(1);
    if (marker === "+") {
      lines.push({ kind: "add", oldNumber: null, newNumber: newNo, text });
      newNo += 1;
      additions += 1;
    } else if (marker === "-") {
      lines.push({ kind: "del", oldNumber: oldNo, newNumber: null, text });
      oldNo += 1;
      deletions += 1;
    } else if (marker === " ") {
      lines.push({ kind: "context", oldNumber: oldNo, newNumber: newNo, text });
      oldNo += 1;
      newNo += 1;
    }
  }

  const path = resolvePath(oldPath, newPath);
  if (path === null) return null;
  return { path, isNew, isDeleted, isBinary, lines, additions, deletions };
}

/** Parse a unified `git diff` patch into a `path → FileDiff` map. */
export function parseUnifiedDiff(unified: string): Map<string, FileDiff> {
  const map = new Map<string, FileDiff>();
  if (!unified.trim()) return map;

  const rows = unified.split("\n");
  let section: string[] | null = null;
  const flush = () => {
    if (section === null) return;
    const file = parseSection(section);
    if (file) map.set(file.path, file);
  };

  for (const row of rows) {
    if (row.startsWith("diff --git ")) {
      flush();
      section = [];
      continue;
    }
    if (section !== null) section.push(row);
  }
  flush();
  return map;
}
