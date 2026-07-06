/**
 * Delivery Report surface (R3 redesign) — the calm, editorial "glass box proof"
 * for one shipped deliverable. Drives the DeliveryReport container with a
 * route-aware mocked fetch and asserts the NEW structure:
 *  - HEADER: a status pill (Verified / Needs review) + the plain title
 *  - "What this did" LEADS with the narrative (falls back to request when null)
 *  - "Note" appears ONLY on a non-passed verification signal (omitted otherwise)
 *  - "How it was verified" renders a CHECKLIST (knowledge excluded), each row a
 *    clean label + a passed tag
 *  - "Knowledge" renders referenced chips (+ a Learned group only when present)
 *  - the diff is BEHIND a collapsed disclosure (NOT shown expanded on load)
 *  - rollback lives in the de-emphasized FOOTER
 *  - calm states: no-verification, not-found (404), inline error, loading
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The collapsed diff panel renders via @git-diff-view/react; mock it to a
// lightweight surface that exposes the hunk text it receives.
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
  narrative:
    "Added a getRelatedPosts helper so each blog post can show a few related reads. It picks posts that share the most tags and skips the post itself.",
  verified: true,
  // Shipped run → the footer shows the Rollback affordance (R8). A held delivery
  // (held_delivery_item_id set) would show Approve & ship / Decline instead.
  run_status: "shipped",
  held_delivery_item_id: null,
  verifications: [
    {
      id: "v1",
      outcome: "passed",
      contract: {
        checks: [
          { kind: "command", command: "pytest -q", rationale: "the suite must pass" },
          { kind: "judge", criteria: ["reads cleanly", "matches the spec"], rationale: "style" },
          // The retriever-folded knowledge check — same rationale the backend
          // extracts into `references`. MUST be filtered out of the verification
          // list (knowledge is reference, not verification).
          {
            kind: "judge",
            criteria: ["Reuse the existing date helper"],
            rationale: "Canonical patterns retrieved for this change",
          },
        ],
      },
      result: { summary: "19 passed" },
      created_at: NOW,
    },
  ],
  references: [
    {
      kind: "concept",
      text: "Reuse the existing date helper",
      concept_id: "reuse-the-existing-date-helper",
    },
  ],
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
  retract?: () => Response;
  safemode?: (url: string) => Response;
  note?: (url: string) => Response;
  concept?: (url: string) => Response;
}) {
  const reportFn = opts?.report ?? (() => REPORT);
  const noteFn =
    opts?.note ??
    (() =>
      json({ path: "garden/seedling/settle-x.md", title: "Note X", content: "# Note X\n\nbody" }));
  const conceptFn =
    opts?.concept ??
    (() => json({ id: "x", name: "X", aliases: [], related: [], observations: [] }));
  const safemodeFn = opts?.safemode ?? (() => json({ item_id: "item-1", status: "approved" }));
  const artifactFn =
    opts?.artifact ??
    ((url: string) =>
      url.includes("src/blog.ts")
        ? json({ ref: "src/blog.ts", content: BLOG_CONTENT, truncated: false, binary: false })
        : json({ ref: "x", content: "// other", truncated: false, binary: false }));
  const retractFn = opts?.retract;
  global.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.includes("/retract")) {
      return (
        retractFn?.() ??
        json({
          deliverable_id: "d1",
          retracted: true,
          retracted_at: NOW,
          already_retracted: false,
          compensated: [{ plugin: "github", artifact_type: "pr", output: {} }],
        })
      );
    }
    if (url.includes("/safemode/")) return safemodeFn(url);
    if (url.includes("/inside/note")) return noteFn(url);
    if (url.includes("/inside/concepts/")) return conceptFn(url);
    if (url.includes("/artifacts/")) return artifactFn(url);
    const r = reportFn();
    return r instanceof Response ? r : json(r);
  }) as unknown as typeof fetch;
}

describe("Delivery Report (R3)", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("leads with the narrative under 'What this did', with a Verified status pill", async () => {
    installFetch();
    render(<DeliveryReport deliverableId="d1" />);

    // Title.
    await waitFor(() => {
      expect(screen.getByText("Add getRelatedPosts to blog.ts")).toBeInTheDocument();
    });

    // Status pill reads Verified.
    expect(screen.getByText(/^verified$/i)).toBeInTheDocument();

    // The narrative LEADS under "What this did".
    const lead = screen.getByRole("region", { name: /what this did/i });
    expect(within(lead).getByText(/getRelatedPosts helper so each blog post/i)).toBeInTheDocument();
  });

  it("falls back to the request under 'What this did' when narrative is null", async () => {
    installFetch({ report: () => ({ ...REPORT, narrative: null }) });
    render(<DeliveryReport deliverableId="d1" />);

    const lead = await screen.findByRole("region", { name: /what this did/i });
    // The founder's original Direction is the fallback lead.
    expect(within(lead).getByText(/getRelatedPosts helper to blog\.ts/i)).toBeInTheDocument();
  });

  it("renders the verification checklist (knowledge excluded) with passed tags", async () => {
    installFetch();
    render(<DeliveryReport deliverableId="d1" />);

    const checks = await screen.findByRole("region", { name: /how it was verified/i });
    // The REAL checks render as checklist rows.
    expect(within(checks).getByText(/pytest -q/)).toBeInTheDocument();
    expect(within(checks).getByText(/reads cleanly/)).toBeInTheDocument();
    // The retrieved-knowledge judge check is filtered OUT (L12).
    expect(within(checks).queryByText(/reuse the existing date helper/i)).toBeNull();
    // Each row carries a "passed" tag.
    expect(within(checks).getAllByText(/passed/i).length).toBeGreaterThan(0);
  });

  it("shows each check's rationale — the why/what — under its command", async () => {
    installFetch();
    render(<DeliveryReport deliverableId="d1" />);

    const checks = await screen.findByRole("region", { name: /how it was verified/i });
    // The command's rationale (why it was run / what it proves) renders as prose
    // under the command label.
    expect(within(checks).getByText("the suite must pass")).toBeInTheDocument();
    expect(within(checks).getByText(/matches the spec/i)).toBeInTheDocument();
  });

  it("omits the rationale line when the agent left it blank", async () => {
    installFetch({
      report: () => ({
        ...REPORT,
        verifications: [
          {
            ...REPORT.verifications[0],
            contract: { checks: [{ kind: "command", command: "pytest -q", rationale: "" }] },
          },
        ],
      }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    const checks = await screen.findByRole("region", { name: /how it was verified/i });
    expect(within(checks).getByText(/pytest -q/)).toBeInTheDocument();
    // No empty rationale paragraph is rendered.
    expect(checks.querySelector(".report-checklist__why")).toBeNull();
  });

  it("OMITS the Note section when the strongest outcome is passed", async () => {
    installFetch();
    render(<DeliveryReport deliverableId="d1" />);

    await screen.findByText("Add getRelatedPosts to blog.ts");
    expect(screen.queryByRole("region", { name: /^note$/i })).toBeNull();
  });

  it("surfaces a quiet Note ONLY when the strongest verification is not passed", async () => {
    installFetch({
      report: () => ({
        ...REPORT,
        deliverable: { ...REPORT.deliverable, verified: false },
        verified: false,
        verifications: [
          {
            id: "v1",
            outcome: "inconclusive",
            contract: { checks: [{ kind: "command", command: "pytest -q", rationale: "" }] },
            result: { error: "toolchain missing" },
            created_at: NOW,
          },
        ],
      }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    const note = await screen.findByRole("region", { name: /^note$/i });
    expect(within(note).getByText(/toolchain missing/i)).toBeInTheDocument();
  });

  it("renders referenced knowledge as chips under 'Knowledge'", async () => {
    installFetch({
      report: () => ({
        ...REPORT,
        references: [
          { kind: "note", text: "Which database?", path: "garden/seedling/settle-db.md" },
        ],
      }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    const knowledge = await screen.findByRole("region", { name: /knowledge/i });
    expect(within(knowledge).getByText(/Which database\?/)).toBeInTheDocument();
  });

  it("hides the Knowledge section when neither referenced nor written exist", async () => {
    installFetch({ report: () => ({ ...REPORT, references: [], written: [] }) });
    render(<DeliveryReport deliverableId="d1" />);

    await screen.findByText("Add getRelatedPosts to blog.ts");
    expect(screen.queryByRole("region", { name: /knowledge/i })).toBeNull();
  });

  it("renders an Added (written) sub-group when report.written is non-empty", async () => {
    installFetch({
      report: () => ({
        ...REPORT,
        references: [],
        written: [{ title: "getRelatedPosts pattern", path: "garden/seedling/settle-grp.md" }],
      }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    const knowledge = await screen.findByRole("region", { name: /knowledge/i });
    expect(within(knowledge).getByText(/getRelatedPosts pattern/)).toBeInTheDocument();
  });

  it("R12: clicking an added-knowledge chip opens the note viewer with its content", async () => {
    installFetch({
      report: () => ({
        ...REPORT,
        references: [],
        written: [{ title: "mean utility", path: "garden/seedling/settle-mean.md" }],
      }),
      note: () =>
        json({
          path: "garden/seedling/settle-mean.md",
          title: "mean utility",
          content: "# mean utility\n\nReturns the arithmetic mean; raises on empty.",
        }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    const knowledge = await screen.findByRole("region", { name: /knowledge/i });
    await userEvent.click(within(knowledge).getByRole("button", { name: /mean utility/i }));

    const dialog = await screen.findByRole("dialog", { name: /mean utility/i });
    expect(within(dialog).getByText(/arithmetic mean; raises on empty/i)).toBeInTheDocument();
  });

  it("a prior-decision reference links to its stored note, opened in the note viewer", async () => {
    installFetch({
      report: () => ({
        ...REPORT,
        references: [
          { kind: "note", text: "Which database?", path: "garden/seedling/settle-db.md" },
        ],
        written: [],
      }),
      note: () =>
        json({
          path: "garden/seedling/settle-db.md",
          title: "Which database?",
          content: "# Decision\n\nResolved: use Postgres for the new service.",
        }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    const knowledge = await screen.findByRole("region", { name: /knowledge/i });
    // The chip is the question (a clickable link, NOT a dead English tag).
    await userEvent.click(within(knowledge).getByRole("button", { name: /Which database\?/ }));

    // Clicking it opens the stored garden note in the note viewer.
    const dialog = await screen.findByRole("dialog", { name: /which database/i });
    expect(within(dialog).getByText(/use Postgres for the new service/i)).toBeInTheDocument();
  });

  it("R13: clicking a concept reference opens the concept viewer with related concepts", async () => {
    installFetch({
      report: () => ({
        ...REPORT,
        references: [{ kind: "concept", text: "Function", concept_id: "function" }],
        written: [],
      }),
      concept: () =>
        json({
          id: "function",
          name: "Function",
          aliases: [],
          related: [{ id: "pure-functions", name: "Pure functions", weight: 3 }],
          observations: [
            {
              id: "garden/seedling/settle-mean.md",
              title: "Add a mean utility",
              excerpt: "",
              body: "",
            },
          ],
        }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    const knowledge = await screen.findByRole("region", { name: /knowledge/i });
    await userEvent.click(within(knowledge).getByRole("button", { name: /^Function$/ }));

    const dialog = await screen.findByRole("dialog", { name: /function/i });
    expect(within(dialog).getByText(/Pure functions/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Add a mean utility/)).toBeInTheDocument();
  });

  it("R15: a related concept navigates the modal; an observation opens its note", async () => {
    installFetch({
      report: () => ({
        ...REPORT,
        references: [{ kind: "concept", text: "Function", concept_id: "function" }],
        written: [],
      }),
      // Route concept detail by id so navigating to a related concept changes content.
      concept: (url: string) =>
        url.includes("pure-functions")
          ? json({
              id: "pure-functions",
              name: "Pure functions",
              aliases: ["Pure function"],
              type: "Pattern",
              related: [],
              observations: [],
            })
          : json({
              id: "function",
              name: "Function",
              aliases: [],
              type: null,
              related: [{ id: "pure-functions", name: "Pure functions", weight: 3 }],
              observations: [
                {
                  id: "garden/seedling/settle-mean.md",
                  title: "Add a mean utility",
                  excerpt: "",
                  body: "",
                },
              ],
            }),
      note: () =>
        json({
          path: "garden/seedling/settle-mean.md",
          title: "Add a mean utility",
          content: "# mean\n\nbody",
        }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    const knowledge = await screen.findByRole("region", { name: /knowledge/i });
    await userEvent.click(within(knowledge).getByRole("button", { name: /^Function$/ }));
    const dialog = await screen.findByRole("dialog");

    // Click the related concept → the SAME modal navigates to it (Pattern badge).
    await userEvent.click(within(dialog).getByRole("button", { name: /Pure functions/ }));
    expect(await within(dialog).findByText("Pattern")).toBeInTheDocument();
  });

  it("R15: clicking an observation opens that note in the note viewer", async () => {
    installFetch({
      report: () => ({
        ...REPORT,
        references: [{ kind: "concept", text: "Function", concept_id: "function" }],
        written: [],
      }),
      concept: () =>
        json({
          id: "function",
          name: "Function",
          aliases: [],
          type: null,
          related: [],
          observations: [
            {
              id: "garden/seedling/settle-mean.md",
              title: "Add a mean utility",
              excerpt: "",
              body: "",
            },
          ],
        }),
      note: () =>
        json({
          path: "garden/seedling/settle-mean.md",
          title: "Add a mean utility",
          content: "# mean\n\nReturns the arithmetic mean.",
        }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    const knowledge = await screen.findByRole("region", { name: /knowledge/i });
    await userEvent.click(within(knowledge).getByRole("button", { name: /^Function$/ }));
    const conceptDialog = await screen.findByRole("dialog", { name: /function/i });
    await userEvent.click(
      within(conceptDialog).getByRole("button", { name: /Add a mean utility/ }),
    );

    const noteDialog = await screen.findByRole("dialog", { name: /add a mean utility/i });
    expect(within(noteDialog).getByText(/arithmetic mean/i)).toBeInTheDocument();
  });

  it("keeps the diff BEHIND a collapsed disclosure (not expanded on load)", async () => {
    installFetch();
    const { container } = render(<DeliveryReport deliverableId="d1" />);

    await screen.findByText("Add getRelatedPosts to blog.ts");
    // The disclosure is present, names the file count, and is NOT open by default.
    const details = container.querySelector("details.report-diff-disclosure");
    expect(details).not.toBeNull();
    expect((details as HTMLDetailsElement).open).toBe(false);
    expect(screen.getByText(/files changed|see what files changed|코드 변경/i)).toBeInTheDocument();
  });

  it("places the rollback affordance in the de-emphasized footer", async () => {
    installFetch();
    render(<DeliveryReport deliverableId="d1" />);

    const footer = await screen.findByRole("contentinfo");
    expect(within(footer).getByRole("button", { name: /roll back|되돌리기/i })).toBeInTheDocument();
  });

  it("R8: a HELD delivery shows Approve / Decline (not Rollback) and dispatches it", async () => {
    const safemode = vi.fn((url: string) => json({ item_id: "item-1", status: "approved" }));
    installFetch({
      report: () => ({ ...REPORT, run_status: "review_ready", held_delivery_item_id: "item-1" }),
      safemode,
    });
    render(<DeliveryReport deliverableId="d1" />);

    const footer = await screen.findByRole("contentinfo");
    // R11: the footer buttons read IDENTICALLY to the Brief card — Approve /
    // Decline (decisions namespace), NOT "Approve & ship". And never Rollback.
    expect(within(footer).queryByRole("button", { name: /roll back|되돌리기/i })).toBeNull();
    const approve = within(footer).getByRole("button", { name: /^Approve$/i });
    expect(within(footer).getByRole("button", { name: /Decline/i })).toBeInTheDocument();

    await userEvent.click(approve);
    await waitFor(() => expect(safemode).toHaveBeenCalled());
    expect(safemode.mock.calls[0][0]).toContain("/api/v1/safemode/item-1/approve");
  });

  it("shows a calm no-verification state when the run has no verification recorded", async () => {
    installFetch({
      report: () => ({
        deliverable: { ...REPORT.deliverable, diff_url: null, verified: false },
        request: null,
        narrative: null,
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
  });

  it("reads 'Needs review' on the status pill when the backend did not certify it", async () => {
    installFetch({
      report: () => ({
        ...REPORT,
        deliverable: { ...REPORT.deliverable, verified: false },
        verified: false,
        verifications: [{ id: "v1", outcome: "passed", contract: {}, result: {}, created_at: NOW }],
      }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    await waitFor(() => {
      expect(screen.getByText("Add getRelatedPosts to blog.ts")).toBeInTheDocument();
    });
    // B4: a stray "passed" row must NOT produce a green Verified pill.
    expect(screen.queryByText(/^verified$/i)).toBeNull();
    expect(screen.getByText(/needs review/i)).toBeInTheDocument();
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

  // ---- L12: knowledge is reference, NOT verification ----

  it("keeps retrieved-knowledge out of the verification list but in Knowledge", async () => {
    installFetch(); // default REPORT carries the knowledge check + matching reference
    render(<DeliveryReport deliverableId="d1" />);

    const checks = await screen.findByRole("region", { name: /how it was verified/i });
    expect(within(checks).getByText(/pytest -q/)).toBeInTheDocument();
    expect(within(checks).queryByText(/reuse the existing date helper/i)).toBeNull();
    // …but it still surfaces under Knowledge.
    const knowledge = screen.getByRole("region", { name: /knowledge/i });
    expect(within(knowledge).getByText(/reuse the existing date helper/i)).toBeInTheDocument();
  });

  it("when the only checks were knowledge, the verified block reads calmly", async () => {
    installFetch({
      report: () => ({
        ...REPORT,
        verifications: [
          {
            id: "v1",
            outcome: "passed",
            contract: {
              checks: [
                {
                  kind: "judge",
                  criteria: ["Reuse the existing date helper"],
                  rationale: "Canonical patterns retrieved for this change",
                },
              ],
            },
            result: {},
            created_at: NOW,
          },
        ],
        references: [
          {
            kind: "concept",
            text: "Reuse the existing date helper",
            concept_id: "reuse-the-existing-date-helper",
          },
        ],
      }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    const checks = await screen.findByRole("region", { name: /how it was verified/i });
    expect(within(checks).queryByText(/reuse the existing date helper/i)).toBeNull();
    expect(within(checks).queryByText(/no checks were declared/i)).toBeNull();
    expect(
      within(checks).getByText(/no additional checks beyond the referenced knowledge/i),
    ).toBeInTheDocument();
  });

  // ---- rollback (footer) ----

  it("rolls back a shipped deliverable from the footer and shows what was reverted", async () => {
    installFetch();
    render(<DeliveryReport deliverableId="d1" />);

    const footer = await screen.findByRole("contentinfo");
    await userEvent.click(within(footer).getByRole("button", { name: /^roll back$/i }));
    await userEvent.click(within(footer).getByRole("button", { name: /^roll back$/i }));

    await waitFor(() => {
      expect(within(footer).getByText(/rolled back — pr/i)).toBeInTheDocument();
    });
    const calls = (global.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(
      calls.some(
        ([url, init]) =>
          String(url).includes("/retract") && (init as RequestInit | undefined)?.method === "POST",
      ),
    ).toBe(true);
  });

  it("hides the rollback affordance for a pure direct_output answer", async () => {
    installFetch({
      report: () => ({
        ...REPORT,
        deliverable: { ...REPORT.deliverable, deliverable_type: "direct_output" },
      }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    await screen.findByText("Add getRelatedPosts to blog.ts");
    expect(screen.queryByRole("button", { name: /^roll back$/i })).toBeNull();
  });

  // ── Verification proof surface — honesty grade, demonstration, failed next-step,
  //    and collapsing the retry wall (founder #2) ────────────────────────────────
  const passedCheck = (command: string) => ({
    kind: "command" as const,
    command,
    rationale: "",
  });

  it("collapses earlier failed verification attempts behind a disclosure (no wall of red)", async () => {
    const failedAttempt = (i: number) => ({
      id: `f${i}`,
      outcome: "failed" as const,
      contract: { checks: [passedCheck(`ruff check attempt-${i}`)] },
      result: {
        command_results: [
          { command: `ruff check attempt-${i}`, passed: false, exit_code: 1, output: "E501" },
        ],
      },
      created_at: NOW,
    });
    installFetch({
      report: () => ({
        ...REPORT,
        verifications: [
          failedAttempt(1),
          failedAttempt(2),
          failedAttempt(3),
          {
            id: "authok",
            outcome: "passed",
            contract: { checks: [passedCheck("pytest -q")] },
            result: {
              command_results: [
                { command: "pytest -q", passed: true, exit_code: 0, output: "3 passed" },
              ],
            },
            honesty_grade: "B",
            created_at: NOW,
          },
        ],
      }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    const checks = await screen.findByRole("region", { name: /how it was verified/i });
    // The authoritative (passing) check leads.
    expect(within(checks).getByText(/pytest -q/)).toBeInTheDocument();
    // The 3 failed retries collapse into a single disclosure summary — NOT a wall.
    expect(within(checks).getByText(/3 earlier attempts/i)).toBeInTheDocument();
  });

  it("shows the honesty grade on a passing verification", async () => {
    installFetch({
      report: () => ({
        ...REPORT,
        verifications: [
          {
            id: "v1",
            outcome: "passed",
            contract: { checks: [passedCheck("pytest -q")] },
            result: {},
            honesty_grade: "B",
            created_at: NOW,
          },
        ],
      }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    const checks = await screen.findByRole("region", { name: /how it was verified/i });
    expect(within(checks).getByText(/evidence\s*b/i)).toBeInTheDocument();
  });

  it("surfaces the outcome-demonstration probes (ran-against-the-result)", async () => {
    installFetch({
      report: () => ({
        ...REPORT,
        verifications: [
          {
            id: "v1",
            outcome: "passed",
            contract: { checks: [passedCheck("pytest -q")] },
            result: {
              outcome_demonstration: {
                verdict: "demonstrated",
                probes: [
                  { name: "double returns n*2", status: "matched" },
                  { name: "handles zero and negatives", status: "matched" },
                ],
              },
            },
            honesty_grade: "B",
            created_at: NOW,
          },
        ],
      }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    const checks = await screen.findByRole("region", { name: /how it was verified/i });
    expect(within(checks).getByText(/double returns n\*2/)).toBeInTheDocument();
  });

  it("explains WHY a failed verification failed and offers a next step (retry the run)", async () => {
    installFetch({
      report: () => ({
        ...REPORT,
        deliverable: { ...REPORT.deliverable, run_id: "run-77", verified: false },
        verified: false,
        run_status: "failed",
        verifications: [
          {
            id: "v1",
            outcome: "failed",
            contract: { checks: [passedCheck("pytest -q")] },
            result: {
              command_results: [
                {
                  command: "pytest -q",
                  passed: false,
                  exit_code: 1,
                  output: "E   assert 3 == 4\n1 failed in 0.01s",
                },
              ],
            },
            created_at: NOW,
          },
        ],
      }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    const checks = await screen.findByRole("region", { name: /how it was verified/i });
    // WHY: the failing command's real output is surfaced, not a bare "failed".
    expect(within(checks).getByText(/assert 3 == 4/)).toBeInTheDocument();
    // NEXT STEP: a link into the producing run (where Retry lives).
    const retry = within(checks).getByRole("link", { name: /retry/i });
    expect(retry).toHaveAttribute("href", "/runs/run-77");
  });

  it("renders a long note reference (a prior decision) as a readable block, not a squished pill", async () => {
    // A LONG decision question renders as a block chip so a rounded pill doesn't
    // push its first/last line outside the border — and it still links to the note.
    installFetch({
      report: () => ({
        ...REPORT,
        references: [
          {
            kind: "note",
            text: "should webhook verification authenticate the exact raw request body with an HMAC and compare signatures using timing-safe equality?",
            path: "garden/seedling/settle-webhook.md",
          },
        ],
        written: [],
      }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    const knowledge = await screen.findByRole("region", { name: /knowledge/i });
    const chip = within(knowledge).getByRole("button", { name: /timing-safe equality/ });
    expect(chip.className).toMatch(/report-chip--statement/);
  });

  it("shows a concept chip as the LABEL, and its BODY appears in the viewer on click", async () => {
    // #1: the chip is the short label; the folded-in body stays out. Clicking it
    // fetches by the backend concept_id (not a re-slugified sentence — the 404
    // regression) and the viewer surfaces the concept's body (observation excerpt).
    installFetch({
      report: () => ({
        ...REPORT,
        references: [
          { kind: "concept", text: "Agent-verification", concept_id: "agent-verification" },
        ],
        written: [],
      }),
      concept: (url: string) =>
        /\/inside\/concepts\/agent-verification($|\?)/.test(url)
          ? json({
              id: "agent-verification",
              name: "Agent-verification",
              aliases: [],
              related: [],
              observations: [
                {
                  id: "garden/seedling/settle-webhook.md",
                  title: "Webhook signatures need body HMAC and replay context",
                  excerpt: "[[Toss Payments]] webhook verification must authenticate the raw body.",
                  body: "[[Toss Payments]] webhook verification must authenticate the raw body.",
                  truncated: false,
                  captured_at: NOW,
                },
              ],
            })
          : json({ detail: "not found" }, 404),
    });
    render(<DeliveryReport deliverableId="d1" />);

    const knowledge = await screen.findByRole("region", { name: /knowledge/i });
    // The chip is the short label — NOT the long "{label} — {body}" statement.
    const chip = within(knowledge).getByRole("button", { name: "Agent-verification" });
    await userEvent.click(chip);

    // The viewer opens (concept_id fetch succeeded) and shows the BODY inline.
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(/\[\[Toss Payments\]\]/)).toBeInTheDocument();
  });

  it("leads 'How it was verified' with the repo's OWN derived gate, not the agent's advisory commands", async () => {
    installFetch({
      report: () => ({
        ...REPORT,
        verifications: [
          {
            id: "v1",
            outcome: "passed",
            // The agent DECLARED an env-incompatible command (F7) — advisory now.
            contract: {
              checks: [
                { kind: "command", command: "uv run --extra dev ruff check", rationale: "" },
              ],
            },
            result: {
              command_results: [
                {
                  command: "uv run --extra dev ruff check",
                  passed: false,
                  exit_code: 2,
                  output: "",
                },
              ],
              // The AUTHORITATIVE gate: the repo's own derived checks.
              derived_gate: {
                applicable: true,
                passed: true,
                commands: [
                  { command: "ruff check money.py", kind: "quality", status: "passed" },
                  { command: "pytest test_money.py", kind: "test", status: "unavailable" },
                ],
              },
            },
            honesty_grade: "A",
            created_at: NOW,
          },
        ],
      }),
    });
    render(<DeliveryReport deliverableId="d1" />);

    const checks = await screen.findByRole("region", { name: /how it was verified/i });
    // The DERIVED gate command (the repo's own check) leads.
    expect(within(checks).getByText("ruff check money.py")).toBeInTheDocument();
    // The agent's invented/advisory command is NOT surfaced as a gating check.
    expect(within(checks).queryByText(/--extra dev/)).toBeNull();
    // A tool that wasn't available reads neutrally (recorded, not a failure).
    expect(within(checks).getByText(/not available/i)).toBeInTheDocument();
  });
});
