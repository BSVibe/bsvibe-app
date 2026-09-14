/**
 * The PWA's schedule-kind vocabulary, pinned to the BACKEND's.
 *
 * Why this file exists (#948): three comments and one test in this app asserted
 * that `product_tick` was "S4, NOT built". That was TRUE when written and became
 * FALSE on 2026-07-21 when PR #609 shipped the kind — prose does not go red when
 * it stops being true, so the claim survived, and a test had even locked it in.
 *
 * So the frontend no longer RESTATES which kinds exist. It declares a constant
 * and this test reads the backend's own `_SUPPORTED_KINDS` off disk and compares.
 * The next person who adds a kind to (or removes one from) that frozenset gets a
 * red PWA test naming the comments they have to update with it.
 *
 * Two axes, deliberately separate — conflating them IS #948:
 *  - what the BACKEND accepts  → `BACKEND_SUPPORTED_SCHEDULE_KINDS` (capability)
 *  - what THIS UI can author   → `PWA_AUTHORABLE_SCHEDULE_KINDS` (surface scope)
 */

import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import {
  BACKEND_SUPPORTED_SCHEDULE_KINDS,
  PWA_AUTHORABLE_SCHEDULE_KINDS,
  SCHEDULE_KIND_INSTRUCTION,
  SCHEDULE_KIND_PRODUCT_TICK,
} from "@/lib/api/schedules";
import { describe, expect, it } from "vitest";

/** Walk up from the vitest root (`apps/pwa`) to the repo root. `import.meta.url`
 *  is NOT a `file:` URL under the jsdom environment, so it cannot be used here.
 *  Throws if the root is not found — a silently wrong root would make every
 *  `readFileSync` below explode anyway, but it should explode with a reason. */
function findRepoRoot(): string {
  let dir = process.cwd();
  for (let hop = 0; hop < 10; hop += 1) {
    if (existsSync(join(dir, "pyproject.toml")) && existsSync(join(dir, "backend"))) return dir;
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  throw new Error(`Could not find the repo root walking up from ${process.cwd()}`);
}

const REPO_ROOT = findRepoRoot();

/** The module that gates the authoring surface. */
const SCHEDULE_SERVICE = "backend/schedule/application/schedule_service.py";

/** Where the kind constants `_SUPPORTED_KINDS` names are actually defined. */
const KIND_CONSTANT_MODULES = [
  "backend/schedule/infrastructure/schedule_db.py",
  "backend/shared/wire_kinds.py",
];

function readRepoFile(relativePath: string): string {
  return readFileSync(join(REPO_ROOT, relativePath), "utf8");
}

/** Resolve a module-level `NAME = "value"` string constant out of the backend
 *  modules that define the schedule-kind vocabulary. Throws rather than
 *  returning a default: a resolver that silently yields nothing would turn this
 *  whole guard green against an empty set. */
function resolveKindConstant(name: string): string {
  for (const relativePath of KIND_CONSTANT_MODULES) {
    const match = readRepoFile(relativePath).match(
      new RegExp(`^${name}\\s*(?::[^=\\n]+)?=\\s*"([^"]+)"`, "m"),
    );
    if (match) return match[1];
  }
  throw new Error(
    `Could not resolve backend constant ${name} in ${KIND_CONSTANT_MODULES.join(", ")} — the kind vocabulary moved; update KIND_CONSTANT_MODULES.`,
  );
}

/** Parse `_SUPPORTED_KINDS` out of the backend service — the single place that
 *  decides which kinds `POST /api/v1/schedules` accepts. */
function backendSupportedKinds(): string[] {
  const block = readRepoFile(SCHEDULE_SERVICE).match(
    /_SUPPORTED_KINDS[^=]*=\s*frozenset\(\s*\{([^}]*)\}/,
  );
  if (!block) {
    throw new Error(
      `Could not locate _SUPPORTED_KINDS in ${SCHEDULE_SERVICE} — it was renamed or restructured; this guard is blind until the parser is updated.`,
    );
  }
  return block[1]
    .split(",")
    .map((token) => token.trim())
    .filter((token) => token.length > 0)
    .map((token) => (/^["']/.test(token) ? token.slice(1, -1) : resolveKindConstant(token)))
    .sort();
}

describe("schedule kinds — the PWA's claims vs the backend's frozenset", () => {
  it("parses a NON-EMPTY kind set out of the backend (guard on the guard)", () => {
    // A parity assertion that passes against `[]` proves nothing. Pin the one
    // kind that has existed since S1 so a silently-failing parse cannot be
    // mistaken for agreement.
    const kinds = backendSupportedKinds();
    expect(kinds.length).toBeGreaterThan(0);
    expect(kinds).toContain(SCHEDULE_KIND_INSTRUCTION);
  });

  it("declares exactly the kinds the backend accepts — no more, no fewer", () => {
    expect(backendSupportedKinds()).toEqual([...BACKEND_SUPPORTED_SCHEDULE_KINDS].sort());
  });

  it("RETRACTED: product_tick IS built — the backend accepts it", () => {
    // The claim #948 retracts. Shipped 2026-07-21 (PR #609) and live in prod.
    // If this ever goes red, `product_tick` was REMOVED from the backend and the
    // comments that now say "authorable via MCP/REST" became false in turn.
    expect(backendSupportedKinds()).toContain(SCHEDULE_KIND_PRODUCT_TICK);
    expect([...BACKEND_SUPPORTED_SCHEDULE_KINDS]).toContain(SCHEDULE_KIND_PRODUCT_TICK);
  });

  it("KEPT: skill / plugin_action really are unbuilt", () => {
    // The other half of #948's four sentences, still TRUE — deleting them along
    // with the false one would have thrown away a correct claim. Now it is
    // machine-checked instead of asserted in prose.
    const kinds = backendSupportedKinds();
    expect(kinds).not.toContain("skill");
    expect(kinds).not.toContain("plugin_action");
  });

  it("authors a SUBSET of what the backend accepts, and says so as UI scope", () => {
    // The whole point of #948: absence from this UI is a statement about the
    // form, not about the capability. Every kind the UI offers must be one the
    // backend takes; the reverse need not hold.
    const kinds = backendSupportedKinds();
    expect(PWA_AUTHORABLE_SCHEDULE_KINDS.length).toBeGreaterThan(0);
    for (const kind of PWA_AUTHORABLE_SCHEDULE_KINDS) {
      expect(kinds).toContain(kind);
    }
    // Today the UI authors `instruction` alone — `product_tick` needs a product
    // picker (it takes no `text` and REQUIRES a `product_id`), a product
    // decision still open in #948. Building that picker must make this line
    // fail loudly rather than pass in silence.
    expect([...PWA_AUTHORABLE_SCHEDULE_KINDS]).toEqual([SCHEDULE_KIND_INSTRUCTION]);
  });
});
