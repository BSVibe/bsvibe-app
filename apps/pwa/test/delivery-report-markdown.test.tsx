/**
 * Delivery Report — rendered Markdown reading view (Lift 3b).
 *
 * A non-developer's deliverable is often a doc/answer with no "before" to diff.
 * For such a file whose name is Markdown, the right panel RENDERS the Markdown
 * (headings, lists, bold) for reading — not a code-diff with `+` gutters. A
 * non-Markdown no-before file keeps the highlighted-additions diff view; an
 * EDITED Markdown file (present in the captured diff) still shows the diff
 * (track-changes), not the rendered reading view.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mock the diff library so we can detect when the panel chose the diff view vs
// the rendered-markdown view. react-markdown is used for real (renders in jsdom).
vi.mock("@git-diff-view/react", () => {
  const React = require("react");
  return {
    DiffModeEnum: { SplitGitHub: 1, SplitGitLab: 2, Split: 3, Unified: 4 },
    DiffView: ({ data }: { data?: { newFile?: { fileName?: string | null } } }) =>
      React.createElement("div", {
        "data-testid": "git-diff-view",
        "data-filename": data?.newFile?.fileName ?? "",
      }),
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

function reportWith(refs: string[], diffUrl: string | null = null): DeliverableReport {
  return {
    deliverable: {
      id: "d1",
      run_id: "r1",
      workspace_id: "ws-1",
      deliverable_type: "direct_output",
      summary: "A written answer",
      artifact_refs: refs,
      artifact_uri: null,
      diff_url: diffUrl,
      verified: true,
      created_at: NOW,
    },
    request: null,
    verified: true,
    verifications: [],
    references: [],
    narrative: null,
  };
}

const MARKDOWN = "# Hello\n\nSome **bold** answer with a list:\n\n- one\n- two\n";
const CODE = "export const x = 1\n";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installFetch(opts: {
  report: DeliverableReport;
  diff?: () => unknown;
  artifact?: (url: string) => Response;
}) {
  const diffFn = opts.diff ?? (() => ({ diff: null, truncated: false }));
  const artifactFn =
    opts.artifact ??
    ((url: string) =>
      url.includes(".md")
        ? json({ ref: "README.md", content: MARKDOWN, truncated: false, binary: false })
        : json({ ref: "app.ts", content: CODE, truncated: false, binary: false }));
  global.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.includes("/diff")) return json(diffFn());
    if (url.includes("/artifacts/")) return artifactFn(url);
    return json(opts.report);
  }) as unknown as typeof fetch;
}

describe("Delivery Report — rendered Markdown reading view", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders a no-before Markdown file as rendered Markdown, not a diff", async () => {
    installFetch({ report: reportWith(["README.md"]) });
    render(<DeliveryReport deliverableId="d1" />);

    const built = await screen.findByRole("region", { name: /what was built/i });
    // The Markdown is rendered for reading: a real heading + list, no diff chrome.
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Hello" })).toBeInTheDocument();
    });
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(within(built).queryByTestId("git-diff-view")).toBeNull();
  });

  it("keeps the highlighted diff view for a no-before NON-Markdown file", async () => {
    installFetch({ report: reportWith(["app.ts"]) });
    render(<DeliveryReport deliverableId="d1" />);

    await screen.findByRole("region", { name: /what was built/i });
    await waitFor(() => {
      expect(screen.getByTestId("git-diff-view")).toBeInTheDocument();
    });
    expect(screen.queryByRole("heading", { name: "Hello" })).toBeNull();
  });

  it("shows the diff (not the reading view) for an EDITED Markdown file", async () => {
    const DIFF = `diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,1 +1,1 @@
-# Old
+# Hello
`;
    installFetch({
      report: reportWith(["README.md"], "https://github.com/x/y/commit/abc"),
      diff: () => ({ diff: DIFF, truncated: false }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    await screen.findByRole("region", { name: /what was built/i });
    // An edit shows the diff (track-changes), so the git-diff-view is used and
    // the Markdown is NOT rendered into a heading.
    await waitFor(() => {
      expect(screen.getByTestId("git-diff-view")).toBeInTheDocument();
    });
    expect(screen.queryByRole("heading", { name: "Hello" })).toBeNull();
  });
});
