/**
 * Delivery Report surface — the "glass box proof" for one shipped deliverable,
 * read as a single Notion-like DOCUMENT. Drives the DeliveryReport container
 * with a route-aware mocked fetch and asserts:
 *  - the masthead (title + type / verdict chips)
 *  - "Your request" renders the founder's Direction (and is absent when null)
 *  - "What was built" auto-opens the first artifact's CONTENT inline, and
 *    switches when another artifact tab is clicked
 *  - "How BSVibe checked this" shows the verdict + the declared contract checks,
 *    rendered defensively from free-form JSON
 *  - a diff link to diff_url when present
 *  - calm states: no-verification, not-found (404), inline error, loading
 *  - artifact content edge cases: truncated note, 404 "unavailable — see diff"
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The "What was built" panel renders via @git-diff-view/react; mock it to a
// lightweight surface that exposes the hunk text it receives, so the document
// tests assert produced content without the heavy library in jsdom.
vi.mock("@git-diff-view/react", () => {
  const React = require("react");
  return {
    DiffModeEnum: { SplitGitHub: 1, SplitGitLab: 2, Split: 3, Unified: 4 },
    DiffView: ({ data }: { data?: { newFile?: { fileName?: string | null }; hunks?: string[] } }) =>
      React.createElement(
        "div",
        { "data-testid": "git-diff-view", "data-filename": data?.newFile?.fileName ?? "" },
        React.createElement("pre", null, (data?.hunks ?? []).join("\n")),
      ),
  };
});

import DeliveryReport from "@/components/deliverables/DeliveryReport";
import type { ArtifactContent, DeliverableReport } from "@/lib/api/types";
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
    deliverable_type: "pr",
    summary: "Add getRelatedPosts to blog.ts",
    artifact_refs: ["src/blog.ts", "tests/blog.test.ts"],
    artifact_uri: "https://github.com/acme/repo/pull/15",
    diff_url: "https://github.com/acme/repo/commit/abc123",
    // B4: backend-authoritative — True only on a PASSED VerificationResult.
    verified: true,
    created_at: NOW,
  },
  request: "Add a getRelatedPosts helper to blog.ts",
  verified: true,
  verifications: [
    {
      id: "v1",
      outcome: "passed",
      contract: {
        checks: [
          { kind: "command", command: "pytest -q", rationale: "the suite must pass" },
          { kind: "judge", criteria: ["reads cleanly", "matches the spec"], rationale: "style" },
        ],
      },
      result: { summary: "19 passed" },
      created_at: NOW,
    },
  ],
  references: [],
};

const BLOG_CONTENT = "export function getRelatedPosts() {\n  return [];\n}\n";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Route-aware fetch: REPORT for the report URL, and each `/artifacts/{ref}`
 *  URL through `artifactResponder` (defaults to the blog.ts content). */
function installFetch(opts?: {
  report?: () => DeliverableReport | Response;
  artifact?: (url: string) => Response;
}) {
  const reportFn = opts?.report ?? (() => REPORT);
  const artifactFn =
    opts?.artifact ??
    ((url: string) =>
      url.includes("src/blog.ts")
        ? json({ ref: "src/blog.ts", content: BLOG_CONTENT, truncated: false, binary: false })
        : json({ ref: "x", content: "// other", truncated: false, binary: false }));
  global.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.includes("/artifacts/")) return artifactFn(url);
    const r = reportFn();
    return r instanceof Response ? r : json(r);
  }) as unknown as typeof fetch;
}

describe("Delivery Report document", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the masthead, request, what-was-built content, checks, and diff", async () => {
    installFetch();
    render(<DeliveryReport deliverableId="d1" />);

    // Masthead title + chips (type + verdict).
    await waitFor(() => {
      expect(screen.getByText("Add getRelatedPosts to blog.ts")).toBeInTheDocument();
    });
    expect(screen.getByText("Pull request")).toBeInTheDocument();

    // "Your request" — the founder's Direction.
    const request = screen.getByRole("region", { name: /your request/i });
    expect(within(request).getByText(/getRelatedPosts helper to blog\.ts/)).toBeInTheDocument();

    // "What was built" — the first artifact's CONTENT shows inline by default.
    const built = screen.getByRole("region", { name: /what was built/i });
    await waitFor(() => {
      expect(within(built).getByText(/export function getRelatedPosts/)).toBeInTheDocument();
    });

    // "How BSVibe checked this" — verdict + the declared contract checks.
    const checks = screen.getByRole("region", { name: /how bsvibe checked this/i });
    expect(within(checks).getByText(/this is verified/i)).toBeInTheDocument();
    expect(within(checks).getByText(/pytest -q/)).toBeInTheDocument();
    expect(within(checks).getByText(/reads cleanly/)).toBeInTheDocument();

    // Diff link.
    expect(screen.getByRole("link", { name: /diff/i })).toHaveAttribute(
      "href",
      "https://github.com/acme/repo/commit/abc123",
    );
  });

  it("switches the shown artifact when another tab is clicked", async () => {
    installFetch({
      artifact: (url) =>
        url.includes("tests/blog.test.ts")
          ? json({
              ref: "tests/blog.test.ts",
              content: "test('related', () => {})",
              truncated: false,
              binary: false,
            })
          : json({ ref: "src/blog.ts", content: BLOG_CONTENT, truncated: false, binary: false }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    const built = await screen.findByRole("region", { name: /what was built/i });
    // First artifact is shown by default.
    await waitFor(() => {
      expect(within(built).getByText(/export function getRelatedPosts/)).toBeInTheDocument();
    });
    // Click the second artifact's tab → its content replaces the first.
    await userEvent.click(within(built).getByRole("button", { name: /tests\/blog\.test\.ts/ }));
    await waitFor(() => {
      expect(within(built).getByText(/test\('related'/)).toBeInTheDocument();
    });
  });

  it("renders the 'What BSVibe referenced' section with the retrieved knowledge", async () => {
    installFetch({
      report: () => ({
        ...REPORT,
        references: [
          "Avoid (prior rejection) — never ship a payment change without a regression test",
          "Prior decision — Q: Which database? A: Use Postgres",
        ],
      }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    const referenced = await screen.findByRole("region", { name: /what bsvibe referenced/i });
    expect(within(referenced).getByText(/never ship a payment change/i)).toBeInTheDocument();
    expect(within(referenced).getByText(/Use Postgres/)).toBeInTheDocument();
  });

  it("hides the 'What BSVibe referenced' section when nothing was retrieved", async () => {
    installFetch(); // REPORT.references === []
    render(<DeliveryReport deliverableId="d1" />);

    // Wait for the document to render, then assert the section is absent.
    await screen.findByText("Add getRelatedPosts to blog.ts");
    expect(screen.queryByRole("region", { name: /what bsvibe referenced/i })).toBeNull();
  });

  it("omits 'Your request' when the report carries no request", async () => {
    installFetch({ report: () => ({ ...REPORT, request: null }) });
    render(<DeliveryReport deliverableId="d1" />);

    await waitFor(() => {
      expect(screen.getByText("Add getRelatedPosts to blog.ts")).toBeInTheDocument();
    });
    expect(screen.queryByRole("region", { name: /your request/i })).not.toBeInTheDocument();
  });

  it("shows a calm no-verification state when the run has no verification recorded", async () => {
    installFetch({
      report: () => ({
        deliverable: { ...REPORT.deliverable, diff_url: null, verified: false },
        request: null,
        verified: false,
        verifications: [],
        references: [],
      }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    await waitFor(() => {
      expect(screen.getByText("Add getRelatedPosts to blog.ts")).toBeInTheDocument();
    });
    expect(screen.getByText(/no verification recorded/i)).toBeInTheDocument();
    // No diff link when diff_url is absent.
    expect(screen.queryByRole("link", { name: /diff/i })).not.toBeInTheDocument();
  });

  it("renders defensively when contract/result JSON is an unexpected shape", async () => {
    installFetch({
      report: () => ({
        deliverable: { ...REPORT.deliverable, verified: false },
        request: null,
        verified: false,
        verifications: [
          {
            id: "v1",
            outcome: "inconclusive",
            contract: {}, // No "checks" array — must not crash.
            result: { error: "toolchain missing" },
            created_at: NOW,
          },
        ],
        references: [],
      }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    await waitFor(() => {
      expect(screen.getByText("Add getRelatedPosts to blog.ts")).toBeInTheDocument();
    });
    const checks = screen.getByRole("region", { name: /how bsvibe checked this/i });
    expect(within(checks).getByText(/inconclusive/i)).toBeInTheDocument();
  });

  it("renders the green 'This is verified' verdict ONLY when backend verified is true", async () => {
    installFetch();
    render(<DeliveryReport deliverableId="d1" />);
    const checks = await screen.findByRole("region", { name: /how bsvibe checked this/i });
    // Backend verified:true → the verdict is the green "This is verified".
    expect(within(checks).getByText(/this is verified/i)).toBeInTheDocument();
  });

  it("never shows 'This is verified' when backend verified is false (defense-in-depth)", async () => {
    // B4: even if a stray verification row claims `passed`, the backend's
    // authoritative `verified: false` flag must win — the founder must NOT see a
    // green "This is verified" on a deliverable the backend did not certify.
    installFetch({
      report: () => ({
        deliverable: { ...REPORT.deliverable, verified: false },
        request: null,
        verified: false,
        verifications: [
          {
            id: "v1",
            outcome: "passed",
            contract: {},
            result: {},
            created_at: NOW,
          },
        ],
        references: [],
      }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    await waitFor(() => {
      expect(screen.getByText("Add getRelatedPosts to blog.ts")).toBeInTheDocument();
    });
    const checks = screen.getByRole("region", { name: /how bsvibe checked this/i });
    expect(within(checks).queryByText(/this is verified/i)).not.toBeInTheDocument();
    // It reads honestly as not-yet-verified instead.
    expect(within(checks).getByText(/not yet verified/i)).toBeInTheDocument();
  });

  it("shows the calm not-found state for an unknown id (404)", async () => {
    installFetch({ report: () => json({ detail: "not found" }, 404) });
    render(<DeliveryReport deliverableId="ghost" />);

    await waitFor(() => {
      expect(
        screen.getByText(/can’t find that report|can't find that report/i),
      ).toBeInTheDocument();
    });
    expect(screen.getByRole("link", { name: /back to the brief/i })).toHaveAttribute(
      "href",
      "/brief",
    );
  });

  it("renders a calm inline error (not a blank page) when the read fails", async () => {
    installFetch({ report: () => json("boom", 500) });
    render(<DeliveryReport deliverableId="d1" />);

    await waitFor(() => {
      expect(
        screen.getByText(/couldn’t load this report|couldn't load this report/i),
      ).toBeInTheDocument();
    });
  });

  it("shows a loading note before the read lands", async () => {
    installFetch();
    render(<DeliveryReport deliverableId="d1" />);

    expect(screen.getByText(/looking at this report/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText("Add getRelatedPosts to blog.ts")).toBeInTheDocument();
    });
  });

  it("shows a truncated note when the produced content was capped", async () => {
    const content: ArtifactContent = {
      ref: "src/blog.ts",
      content: "partial…",
      truncated: true,
      binary: false,
    };
    installFetch({ artifact: () => json(content) });
    render(<DeliveryReport deliverableId="d1" />);

    const built = await screen.findByRole("region", { name: /what was built/i });
    await waitFor(() => {
      expect(within(built).getByText(/showing the first part|truncated/i)).toBeInTheDocument();
    });
  });

  it("degrades to 'content unavailable — see the diff' on a 404 artifact read", async () => {
    installFetch({ artifact: () => json({ detail: "gone" }, 404) });
    render(<DeliveryReport deliverableId="d1" />);

    const built = await screen.findByRole("region", { name: /what was built/i });
    await waitFor(() => {
      expect(
        within(built).getByText(/couldn’t show this file|couldn't show this file|unavailable/i),
      ).toBeInTheDocument();
    });
    // The diff link is still offered as the fallback path.
    expect(screen.getByRole("link", { name: /diff/i })).toBeInTheDocument();
  });
});
