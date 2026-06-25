/**
 * Delivery Report — "What was built" viewer (Lift 3a, @git-diff-view/react).
 *
 * The left FILE LIST + right panel split is unchanged; the right panel is now
 * rendered by @git-diff-view/react (mocked here so we assert OUR integration —
 * which file, and what diff data we feed it — not the library's internals):
 *  - a file WITH a captured diff → the library gets the real `git diff` hunk,
 *  - a file WITHOUT one → the library gets a synthesized all-additions hunk of
 *    the fetched content,
 *  - switching a file swaps only the right panel; the list persists + current
 *    marker moves,
 *  - a lone file shows no rail.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mock the heavy diff library: render the filename + theme + the hunk text it
// receives, so we can assert our integration contract deterministically.
vi.mock("@git-diff-view/react", () => {
  const React = require("react");
  return {
    DiffModeEnum: { SplitGitHub: 1, SplitGitLab: 2, Split: 3, Unified: 4 },
    DiffView: ({
      data,
      diffViewTheme,
    }: {
      data?: { newFile?: { fileName?: string | null }; hunks?: string[] };
      diffViewTheme?: string;
    }) =>
      React.createElement(
        "div",
        {
          "data-testid": "git-diff-view",
          "data-filename": data?.newFile?.fileName ?? "",
          "data-theme": diffViewTheme ?? "",
        },
        React.createElement("pre", null, (data?.hunks ?? []).join("\n")),
      ),
  };
});

import DeliveryReport from "@/components/deliverables/DeliveryReport";
import type { DeliverableReport } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const NOW = "2026-05-23T00:00:00Z";

const REPORT: DeliverableReport = {
  deliverable: {
    id: "d1",
    run_id: "r1",
    workspace_id: "ws-1",
    deliverable_type: "code",
    summary: "Add getRelatedPosts to blog.ts",
    artifact_refs: ["src/blog.ts", "tests/blog.test.ts"],
    artifact_uri: null,
    diff_url: null,
    verified: true,
    created_at: NOW,
  },
  request: null,
  verified: true,
  verifications: [],
  references: [],
};

const BLOG_CONTENT = "export function getRelatedPosts() {\n  return [];\n}\n";
const TEST_CONTENT = "test('related', () => {})\n";
// A captured diff that touches ONLY src/blog.ts.
const DIFF = `diff --git a/src/blog.ts b/src/blog.ts
--- a/src/blog.ts
+++ b/src/blog.ts
@@ -1,1 +1,1 @@
-export const old = 1
+export const next = 2
`;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installFetch(opts?: { diff?: () => unknown; artifact?: (url: string) => Response }) {
  const diffFn = opts?.diff ?? (() => ({ diff: null, truncated: false }));
  const artifactFn =
    opts?.artifact ??
    ((url: string) =>
      url.includes("tests/blog.test.ts")
        ? json({
            ref: "tests/blog.test.ts",
            content: TEST_CONTENT,
            truncated: false,
            binary: false,
          })
        : json({ ref: "src/blog.ts", content: BLOG_CONTENT, truncated: false, binary: false }));
  global.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.includes("/diff")) return json(diffFn());
    if (url.includes("/artifacts/")) return artifactFn(url);
    return json(REPORT);
  }) as unknown as typeof fetch;
}

function panel() {
  return document.querySelector('[data-testid="git-diff-view"]');
}

describe("Delivery Report — git-diff-view viewer", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders a left file list with every artifact, the first marked current", async () => {
    installFetch();
    render(<DeliveryReport deliverableId="d1" />);

    const built = await screen.findByRole("region", { name: /what was built/i });
    const files = within(built).getByRole("navigation", { name: /files/i });
    expect(within(files).getByRole("button", { name: /src\/blog\.ts/ })).toHaveAttribute(
      "aria-current",
      "true",
    );
    expect(
      within(files).getByRole("button", { name: /tests\/blog\.test\.ts/ }),
    ).toBeInTheDocument();
  });

  it("feeds the library the real captured diff hunk for a file in the diff", async () => {
    installFetch({ diff: () => ({ diff: DIFF, truncated: false }) });
    render(<DeliveryReport deliverableId="d1" />);

    await screen.findByRole("region", { name: /what was built/i });
    await waitFor(() => {
      expect(panel()?.getAttribute("data-filename")).toBe("src/blog.ts");
    });
    // The hunk carries the true old/new lines (not a synthesized additions block).
    expect(panel()?.textContent).toContain("-export const old = 1");
    expect(panel()?.textContent).toContain("+export const next = 2");
  });

  it("feeds a synthesized additions hunk of the fetched content for a file NOT in the diff", async () => {
    installFetch(); // diff: null → additions fallback
    render(<DeliveryReport deliverableId="d1" />);

    await screen.findByRole("region", { name: /what was built/i });
    await waitFor(() => {
      // The first file's content arrives as additions (every line a `+`).
      expect(panel()?.textContent).toContain("+export function getRelatedPosts() {");
    });
  });

  it("updates only the right panel when another file is selected (list persists, current moves)", async () => {
    installFetch();
    render(<DeliveryReport deliverableId="d1" />);

    const built = await screen.findByRole("region", { name: /what was built/i });
    const files = within(built).getByRole("navigation", { name: /files/i });
    await waitFor(() => expect(panel()?.getAttribute("data-filename")).toBe("src/blog.ts"));

    await userEvent.click(within(files).getByRole("button", { name: /tests\/blog\.test\.ts/ }));

    await waitFor(() => expect(panel()?.getAttribute("data-filename")).toBe("tests/blog.test.ts"));
    expect(panel()?.textContent).toContain("+test('related'");
    // List persists; current moved.
    expect(within(files).getByRole("button", { name: /src\/blog\.ts/ })).not.toHaveAttribute(
      "aria-current",
      "true",
    );
    expect(within(files).getByRole("button", { name: /tests\/blog\.test\.ts/ })).toHaveAttribute(
      "aria-current",
      "true",
    );
  });

  it("does not render the file list for a single-artifact deliverable", async () => {
    global.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/diff")) return json({ diff: null, truncated: false });
      if (url.includes("/artifacts/"))
        return json({ ref: "src/blog.ts", content: BLOG_CONTENT, truncated: false, binary: false });
      return json({
        ...REPORT,
        deliverable: { ...REPORT.deliverable, artifact_refs: ["src/blog.ts"] },
      });
    }) as unknown as typeof fetch;
    render(<DeliveryReport deliverableId="d1" />);

    const built = await screen.findByRole("region", { name: /what was built/i });
    await waitFor(() => expect(panel()).not.toBeNull());
    expect(within(built).queryByRole("navigation", { name: /files/i })).toBeNull();
  });
});
