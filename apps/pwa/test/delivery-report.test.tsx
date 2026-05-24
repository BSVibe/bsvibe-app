/**
 * Delivery Report surface — the "glass box proof" for one shipped deliverable.
 * Drives the DeliveryReport container with a mocked fetch and asserts:
 *  - the verdict (verification outcome) renders
 *  - "How BSVibe checked this" lists the declared contract checks + per-check
 *    result, rendered defensively from free-form JSON
 *  - the artifacts (artifact_refs + artifact_uri) render
 *  - a diff link to diff_url when present
 *  - a calm "no verification recorded" state when the run has no verification
 *  - a calm not-found state for an unknown id (404 → not-found, not an error)
 *  - a calm inline error (not a blank page) when the read fails
 *  - a loading note before the read lands
 */

import DeliveryReport from "@/components/deliverables/DeliveryReport";
import type { DeliverableReport } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
    created_at: NOW,
  },
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
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("Delivery Report surface", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the verdict, the declared checks, the artifacts, and a diff link", async () => {
    global.fetch = vi.fn(async () => json(REPORT)) as unknown as typeof fetch;

    render(<DeliveryReport deliverableId="d1" />);

    // Summary header.
    await waitFor(() => {
      expect(screen.getByText("Add getRelatedPosts to blog.ts")).toBeInTheDocument();
    });

    // Verdict — the verification outcome.
    const verdict = screen.getByRole("region", { name: /verdict/i });
    expect(within(verdict).getByText(/verified/i)).toBeInTheDocument();

    // "How BSVibe checked this" — the declared contract checks.
    const checks = screen.getByRole("region", { name: /how bsvibe checked this/i });
    expect(within(checks).getByText(/pytest -q/)).toBeInTheDocument();
    expect(within(checks).getByText(/reads cleanly/)).toBeInTheDocument();
    expect(within(checks).getByText(/matches the spec/)).toBeInTheDocument();

    // Artifacts — refs + external link.
    const artifacts = screen.getByRole("region", { name: /artifacts/i });
    expect(within(artifacts).getByText(/src\/blog\.ts/)).toBeInTheDocument();
    expect(within(artifacts).getByText(/tests\/blog\.test\.ts/)).toBeInTheDocument();
    expect(within(artifacts).getByRole("link", { name: /open/i })).toHaveAttribute(
      "href",
      "https://github.com/acme/repo/pull/15",
    );

    // Diff link.
    expect(screen.getByRole("link", { name: /diff/i })).toHaveAttribute(
      "href",
      "https://github.com/acme/repo/commit/abc123",
    );
  });

  it("shows a calm no-verification state when the run has no verification recorded", async () => {
    const noVerify: DeliverableReport = {
      deliverable: { ...REPORT.deliverable, diff_url: null },
      verifications: [],
    };
    global.fetch = vi.fn(async () => json(noVerify)) as unknown as typeof fetch;

    render(<DeliveryReport deliverableId="d1" />);

    await waitFor(() => {
      expect(screen.getByText("Add getRelatedPosts to blog.ts")).toBeInTheDocument();
    });
    expect(screen.getByText(/no verification recorded/i)).toBeInTheDocument();
    // No diff link when diff_url is absent.
    expect(screen.queryByRole("link", { name: /diff/i })).not.toBeInTheDocument();
  });

  it("renders defensively when contract/result JSON is an unexpected shape", async () => {
    const odd: DeliverableReport = {
      deliverable: { ...REPORT.deliverable },
      verifications: [
        {
          id: "v1",
          outcome: "inconclusive",
          // No "checks" array — must not crash.
          contract: {},
          result: { error: "toolchain missing" },
          created_at: NOW,
        },
      ],
    };
    global.fetch = vi.fn(async () => json(odd)) as unknown as typeof fetch;

    render(<DeliveryReport deliverableId="d1" />);

    await waitFor(() => {
      expect(screen.getByText("Add getRelatedPosts to blog.ts")).toBeInTheDocument();
    });
    // Outcome still renders even with no parseable checks.
    const verdict = screen.getByRole("region", { name: /verdict/i });
    expect(within(verdict).getByText(/inconclusive|couldn|not/i)).toBeInTheDocument();
  });

  it("shows the calm not-found state for an unknown id (404)", async () => {
    global.fetch = vi.fn(async () => json({ detail: "not found" }, 404)) as unknown as typeof fetch;

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
    global.fetch = vi.fn(async () => json("boom", 500)) as unknown as typeof fetch;

    render(<DeliveryReport deliverableId="d1" />);

    await waitFor(() => {
      expect(
        screen.getByText(/couldn’t load this report|couldn't load this report/i),
      ).toBeInTheDocument();
    });
  });

  it("shows a loading note before the read lands", async () => {
    global.fetch = vi.fn(async () => json(REPORT)) as unknown as typeof fetch;

    render(<DeliveryReport deliverableId="d1" />);

    expect(screen.getByText(/looking at this report/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText("Add getRelatedPosts to blog.ts")).toBeInTheDocument();
    });
  });
});
