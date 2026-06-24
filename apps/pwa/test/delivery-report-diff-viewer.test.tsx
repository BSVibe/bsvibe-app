/**
 * Delivery Report — "What was built" GitHub-style diff viewer (Lift 1).
 *
 * The old top-tabs + single content pane is replaced by a GitHub PR-review
 * layout: a left FILE LIST + a right DIFF PANEL. Switching a file updates only
 * the right panel (the file list persists, the current marker moves). File
 * content renders as GitHub-style ADDITIONS — every line is a green "+" row with
 * a line-number gutter (content-as-additions: a freshly produced file is all-new,
 * so each line reads honestly as an addition; true red/green for modified files
 * is a follow-up lift).
 *
 * These assertions are layout/behaviour contracts on top of the existing
 * delivery-report.test.tsx document tests (which still hold).
 */

import DeliveryReport from "@/components/deliverables/DeliveryReport";
import type { DeliverableReport } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installFetch(opts?: {
  report?: () => DeliverableReport | Response;
  artifact?: (url: string) => Response;
}) {
  const reportFn = opts?.report ?? (() => REPORT);
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
    if (url.includes("/artifacts/")) return artifactFn(url);
    const r = reportFn();
    return r instanceof Response ? r : json(r);
  }) as unknown as typeof fetch;
}

describe("Delivery Report — GitHub-style diff viewer", () => {
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
    // The left rail is a navigation listing every produced file as a button.
    const files = within(built).getByRole("navigation", { name: /files/i });
    expect(within(files).getByRole("button", { name: /src\/blog\.ts/ })).toBeInTheDocument();
    expect(
      within(files).getByRole("button", { name: /tests\/blog\.test\.ts/ }),
    ).toBeInTheDocument();
    // The first file is the current/active one by default.
    expect(within(files).getByRole("button", { name: /src\/blog\.ts/ })).toHaveAttribute(
      "aria-current",
      "true",
    );
  });

  it("renders the selected file content as green additions with a line-number gutter", async () => {
    installFetch();
    const { container } = render(<DeliveryReport deliverableId="d1" />);

    const built = await screen.findByRole("region", { name: /what was built/i });
    await waitFor(() => {
      expect(within(built).getByText(/export function getRelatedPosts/)).toBeInTheDocument();
    });
    // Content is rendered as a diff: every line is an "addition" row.
    const addLines = container.querySelectorAll(".report-diff__line--add");
    // BLOG_CONTENT has 3 non-empty lines (trailing newline dropped).
    expect(addLines.length).toBe(3);
    // A line-number gutter starts at 1.
    const firstNum = container.querySelector(".report-diff__num");
    expect(firstNum?.textContent).toBe("1");
    // Each addition carries the "+" marker.
    expect(container.querySelector(".report-diff__marker")?.textContent).toBe("+");
  });

  it("updates only the right panel when another file is selected (list persists, current moves)", async () => {
    installFetch();
    render(<DeliveryReport deliverableId="d1" />);

    const built = await screen.findByRole("region", { name: /what was built/i });
    const files = within(built).getByRole("navigation", { name: /files/i });
    await waitFor(() => {
      expect(within(built).getByText(/export function getRelatedPosts/)).toBeInTheDocument();
    });

    // Switch to the second file.
    await userEvent.click(within(files).getByRole("button", { name: /tests\/blog\.test\.ts/ }));

    // Right panel now shows the second file's content; the first file's content is gone.
    await waitFor(() => {
      expect(within(built).getByText(/test\('related'/)).toBeInTheDocument();
    });
    expect(within(built).queryByText(/export function getRelatedPosts/)).toBeNull();

    // The file list persists — both files still listed — and current has moved.
    expect(within(files).getByRole("button", { name: /src\/blog\.ts/ })).toBeInTheDocument();
    expect(within(files).getByRole("button", { name: /tests\/blog\.test\.ts/ })).toHaveAttribute(
      "aria-current",
      "true",
    );
    expect(within(files).getByRole("button", { name: /src\/blog\.ts/ })).not.toHaveAttribute(
      "aria-current",
      "true",
    );
  });

  it("shows an additions count for the selected file (GitHub-style +N)", async () => {
    installFetch();
    const { container } = render(<DeliveryReport deliverableId="d1" />);

    await screen.findByRole("region", { name: /what was built/i });
    await waitFor(() => {
      expect(container.querySelector(".report-diff__added")?.textContent).toContain("+3");
    });
  });

  it("does not render the file list for a single-artifact deliverable (just the panel)", async () => {
    installFetch({
      report: () => ({
        ...REPORT,
        deliverable: { ...REPORT.deliverable, artifact_refs: ["src/blog.ts"] },
      }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    const built = await screen.findByRole("region", { name: /what was built/i });
    await waitFor(() => {
      expect(within(built).getByText(/export function getRelatedPosts/)).toBeInTheDocument();
    });
    // A lone file needs no rail — keep the single-file view calm.
    expect(within(built).queryByRole("navigation", { name: /files/i })).toBeNull();
  });
});
