/**
 * Decisions surface — the redesigned inbox (Stitch screens
 * 1175801d… Inbox + 5bf54bdf… detail). Driven by a route-aware mocked fetch.
 *
 * The surface is the SINGLE place for everything needing the founder's
 * judgment — three real backend queues aggregated client-side into one calm
 * Pending list, each row labeled by kind:
 *  - "delivery"  ← GET /api/v1/safemode/queue   (held outbound deliveries;
 *                  Approve POSTs /approve, Deny POSTs /deny with { reason })
 *  - "decision"  ← GET /api/v1/checkpoints       (paused-run questions;
 *                  resolve POSTs /resolve with { answer })
 *  - "knowledge" ← GET /api/v1/decisions?status_filter=pending  (canon
 *                  proposals; Accept POSTs /accept, Reject POSTs /reject)
 * The Resolved tab folds the SAME three queues' settled items: decided
 * Safe-Mode deliveries (GET /api/v1/safemode/resolved) + answered checkpoints
 * (GET /api/v1/checkpoints/resolved) + the canon decision log (GET
 * /api/v1/decisions/log).
 *
 * Verified here:
 *  - tab labels carry live counts (Pending = all three kinds summed)
 *  - the Pending count matches the Brief "Needs you" count for the overlap
 *    (deliveries + proposals)
 *  - each kind renders with its label + the right resolve affordance, wired to
 *    its OWN endpoint
 *  - a client-side search box filters the visible list
 *  - opening a knowledge item shows a calm detail/resolve panel (Accept/Reject)
 *  - a forced error keeps the action actionable with a calm message
 *  - the empty Pending state stays calm
 *  - the nav pending-count store reflects the full pending size (all kinds)
 */

import Decisions from "@/components/decisions/Decisions";
import type {
  Checkpoint,
  DecisionLogEntry,
  Deliverable,
  Product,
  Proposal,
  ResolvedCheckpointItem,
  Run,
  SafeModeItem,
  SafeModeResolvedItem,
} from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { setPendingDecisionsCount, usePendingDecisionsCount } from "@/lib/decisions/pending-count";
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

const PROPOSAL: Proposal = {
  // `id` is the proposal's vault path — the accept/reject handle (a `:path`).
  id: "proposals/merge-concepts/2026-05-23-self-hosting.md",
  proposal_kind: "merge",
  action_kind: "merge-concepts",
  action_path: "actions/merge-concepts/2026-05-23-self-hosting.md",
  status: "pending",
  score: 82,
  created_at: "2026-05-23T00:00:00Z",
  expires_at: null,
};

const PROPOSAL_2: Proposal = {
  id: "proposals/create-concept/2026-05-22-jwt-bearer.md",
  proposal_kind: "create",
  action_kind: "create-concept",
  action_path: "actions/create-concept/2026-05-22-jwt-bearer.md",
  status: "pending",
  score: 64,
  created_at: "2026-05-22T00:00:00Z",
  expires_at: null,
};

const DECISION: DecisionLogEntry = {
  id: "decisions/must-link/2026-05-20-css-in-js.md",
  proposal_id: "proposals/merge-concepts/2026-05-20-css-in-js.md",
  decision_kind: "must-link",
  actor_id: "user-1",
  created_at: "2026-05-20T00:00:00Z",
};

const RESOLVED_DELIVERY: SafeModeResolvedItem = {
  id: "33333333-3333-3333-3333-333333333333",
  deliverable_id: "del-9",
  status: "approved",
  decided_at: "2026-05-24T09:00:00Z",
  created_at: "2026-05-24T08:00:00Z",
};

const RESOLVED_CHECKPOINT: ResolvedCheckpointItem = {
  id: "44444444-4444-4444-4444-444444444444",
  run_id: "run-7",
  question: "Ship to staging first?",
  resolution: "yes, staging then prod",
  resolved_at: "2026-05-24T10:00:00Z",
};

const SAFEMODE: SafeModeItem = {
  id: "11111111-1111-1111-1111-111111111111",
  workspace_id: "ws-1",
  deliverable_id: "del-1",
  run_id: null,
  status: "pending",
  compensation_tier: null,
  expires_at: "2026-05-24T00:00:00Z",
  extension_count: 0,
  created_at: "2026-05-23T12:00:00Z",
};

const CHECKPOINT: Checkpoint = {
  id: "22222222-2222-2222-2222-222222222222",
  run_id: "run-1",
  decision: "clarify-scope",
  question: "Should the export include archived items?",
  options: null,
  actions: null,
  rationale: "The direction was ambiguous.",
  prior_decisions: [],
  created_at: "2026-05-23T11:00:00Z",
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * A route-aware fetch mock for the unified Decisions surface. Each list getter
 * returns the contents of one of the three real queues (mutate them between
 * calls to simulate a re-read dropping a resolved item); they all default to
 * empty so a test only declares the kinds it cares about. `onPost` records
 * resolve calls (approve/deny/resolve/accept/reject all route here).
 */
function installFetch(opts: {
  proposals?: () => Proposal[];
  safemode?: () => SafeModeItem[];
  checkpoints?: () => Checkpoint[];
  decisions?: () => DecisionLogEntry[];
  resolvedSafemode?: () => SafeModeResolvedItem[];
  resolvedCheckpoints?: () => ResolvedCheckpointItem[];
  runs?: () => Run[];
  deliverables?: () => Deliverable[];
  products?: () => Product[];
  onPost?: (url: string, init: RequestInit) => Response;
}) {
  const proposals = opts.proposals ?? (() => []);
  const safemode = opts.safemode ?? (() => []);
  const checkpoints = opts.checkpoints ?? (() => []);
  const resolvedSafemode = opts.resolvedSafemode ?? (() => []);
  const resolvedCheckpoints = opts.resolvedCheckpoints ?? (() => []);
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    const method = (init.method ?? "GET").toUpperCase();
    if (method === "GET" && url.startsWith("/api/v1/decisions/log")) {
      return json((opts.decisions ?? (() => []))());
    }
    if (method === "GET" && url.startsWith("/api/v1/decisions?")) return json(proposals());
    // The resolved variants MUST be matched before their pending prefixes
    // (`/checkpoints/resolved` startsWith `/checkpoints`).
    if (method === "GET" && url.startsWith("/api/v1/safemode/resolved")) {
      return json(resolvedSafemode());
    }
    if (method === "GET" && url.startsWith("/api/v1/safemode/queue")) return json(safemode());
    if (method === "GET" && url.startsWith("/api/v1/checkpoints/resolved")) {
      return json(resolvedCheckpoints());
    }
    if (method === "GET" && url.startsWith("/api/v1/checkpoints")) return json(checkpoints());
    // Review-context join (lib/api/review-context) — the Decisions aggregator
    // now fetches these to title each row + link its proof. Default to empty so
    // rows fall back to their generic question (the join is best-effort).
    if (method === "GET" && url.startsWith("/api/v1/runs"))
      return json((opts.runs ?? (() => []))());
    if (method === "GET" && url.startsWith("/api/v1/deliverables")) {
      return json((opts.deliverables ?? (() => []))());
    }
    if (method === "GET" && url.startsWith("/api/v1/products")) {
      return json((opts.products ?? (() => []))());
    }
    if (method === "POST" && opts.onPost) return opts.onPost(url, init);
    throw new Error(`unexpected fetch ${method} ${url}`);
  });
  global.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

describe("Decisions surface", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
    setPendingDecisionsCount(0);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders Pending and Resolved tabs with live counts", async () => {
    installFetch({ proposals: () => [PROPOSAL, PROPOSAL_2], decisions: () => [DECISION] });

    render(<Decisions />);

    const tablist = await screen.findByRole("tablist");
    const tabs = within(tablist);
    // Pending tab shows the 2-proposal count.
    expect(tabs.getByRole("tab", { name: /Pending/ })).toHaveTextContent("2");
    expect(tabs.getByRole("tab", { name: /Resolved/ })).toHaveTextContent("1");
  });

  it("aggregates all three kinds — deliveries, decisions, knowledge — into one Pending count", async () => {
    installFetch({
      proposals: () => [PROPOSAL],
      safemode: () => [SAFEMODE],
      checkpoints: () => [CHECKPOINT],
      decisions: () => [DECISION],
    });

    render(<Decisions />);

    const tablist = await screen.findByRole("tablist");
    // 1 delivery + 1 checkpoint + 1 proposal = 3.
    expect(within(tablist).getByRole("tab", { name: /Pending/ })).toHaveTextContent("3");
  });

  it("renders each pending kind with its own label and question", async () => {
    installFetch({
      proposals: () => [PROPOSAL],
      safemode: () => [SAFEMODE],
      checkpoints: () => [CHECKPOINT],
    });

    render(<Decisions />);

    // delivery (Safe Mode held delivery) — its amber status badge + plain line
    expect(await screen.findByText("Ready to ship")).toBeInTheDocument();
    expect(screen.getByText(/A delivery is held in Safe Mode/)).toBeInTheDocument();
    // decision (paused-run checkpoint) carries the agent's blocking question +
    // its amber status badge
    expect(screen.getByText(/Should the export include archived items\?/)).toBeInTheDocument();
    expect(screen.getByText("Needs your answer")).toBeInTheDocument();
    // knowledge (canon proposal) — verb + the Knowledge kind chip is the
    // proposal's existing proposal_kind chip ("merge")
    expect(screen.getByText(/merge concepts/)).toBeInTheDocument();
  });

  it("titles a held delivery with its task + links to the proof (no blind approval)", async () => {
    // The review-context join: the delivery's deliverable → run → product, so
    // the founder sees WHAT is shipping and can open the proof before approving.
    installFetch({
      safemode: () => [SAFEMODE],
      deliverables: () =>
        [
          {
            id: "del-1",
            run_id: "run-9",
            workspace_id: "ws-1",
            deliverable_type: "pr",
            summary: "Add factorial(n) utility with ValueError on negatives.",
            artifact_refs: [],
            artifact_uri: null,
            verified: true,
            created_at: "2026-05-23T12:00:00Z",
          },
        ] as Deliverable[],
      runs: () =>
        [
          {
            id: "run-9",
            workspace_id: "ws-1",
            product_id: "prod-1",
            request_id: null,
            status: "review_ready",
            intent: "Add a factorial utility",
            created_at: "2026-05-23T12:00:00Z",
            updated_at: "2026-05-23T12:00:00Z",
          },
        ] as Run[],
      products: () =>
        [
          {
            id: "prod-1",
            workspace_id: "ws-1",
            name: "bsvibe-app",
            slug: "bsvibe-app",
            repo_url: null,
            created_at: "2026-05-23T12:00:00Z",
            updated_at: "2026-05-23T12:00:00Z",
          },
        ] as Product[],
    });

    render(<Decisions />);

    // Concise title (the deliverable's summary), not just the generic question.
    expect(
      await screen.findByText("Add factorial(n) utility with ValueError on negatives."),
    ).toBeInTheDocument();
    // A "view proof" link pointing at the deliverable detail.
    const proof = screen.getByRole("link", { name: /View report/ });
    expect(proof).toHaveAttribute("href", "/deliverables/del-1");
    // The product chip.
    expect(screen.getByText("bsvibe-app")).toBeInTheDocument();
  });

  it("approves a held delivery against the safemode endpoint", async () => {
    let safemode = [SAFEMODE];
    const posts: Array<[string, RequestInit]> = [];
    installFetch({
      safemode: () => safemode,
      onPost: (url, init) => {
        posts.push([url, init]);
        safemode = [];
        return json({ item_id: SAFEMODE.id, status: "approved", dispatched: true });
      },
    });

    render(<Decisions />);
    await userEvent.click(await screen.findByRole("button", { name: /Approve/ }));

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /Pending/ })).toHaveTextContent("0");
    });
    const [url, init] = posts[0];
    expect(url).toBe(`/api/v1/safemode/${SAFEMODE.id}/approve`);
    expect(init.method).toBe("POST");
  });

  // ─────────────────────────────────────────────────────────────────────
  // B12a — per-Run delivery grouping + "Approve all (N)" bulk action.
  // ─────────────────────────────────────────────────────────────────────

  it("renders a per-Run group with Approve all when multiple deliveries share a runId", async () => {
    const RUN_ID = "run-7";
    const ITEM_A: SafeModeItem = { ...SAFEMODE, id: "sm-a", run_id: RUN_ID };
    const ITEM_B: SafeModeItem = {
      ...SAFEMODE,
      id: "sm-b",
      deliverable_id: "del-2",
      run_id: RUN_ID,
    };
    let safemode = [ITEM_A, ITEM_B];
    const posts: Array<[string, RequestInit]> = [];
    installFetch({
      safemode: () => safemode,
      onPost: (url, init) => {
        posts.push([url, init]);
        safemode = [];
        return json({ run_id: RUN_ID, approved_count: 2, dispatched_count: 2 });
      },
    });

    render(<Decisions />);
    // The "Approve all (2)" group action appears alongside the per-item rows.
    const approveAll = await screen.findByRole("button", { name: /Approve all \(2\)/ });
    await userEvent.click(approveAll);

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /Pending/ })).toHaveTextContent("0");
    });
    const [url, init] = posts[0];
    expect(url).toBe(`/api/v1/safemode/runs/${RUN_ID}/approve`);
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
  });

  it("does NOT render a per-Run group when only one delivery row has a runId", async () => {
    const SOLO: SafeModeItem = { ...SAFEMODE, run_id: "run-9" };
    installFetch({ safemode: () => [SOLO] });

    render(<Decisions />);
    await screen.findByRole("button", { name: /Approve$/ });
    // No "Approve all" surface for a single-item run.
    expect(screen.queryByRole("button", { name: /Approve all/ })).toBeNull();
  });

  it("denies a held delivery against the safemode endpoint", async () => {
    let safemode = [SAFEMODE];
    const posts: Array<[string, RequestInit]> = [];
    installFetch({
      safemode: () => safemode,
      onPost: (url, init) => {
        posts.push([url, init]);
        safemode = [];
        return json({ item_id: SAFEMODE.id, status: "denied", dispatched: false });
      },
    });

    render(<Decisions />);
    await userEvent.click(await screen.findByRole("button", { name: /Decline|Deny/ }));

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /Pending/ })).toHaveTextContent("0");
    });
    const [url, init] = posts[0];
    expect(url).toBe(`/api/v1/safemode/${SAFEMODE.id}/deny`);
    expect(JSON.parse(init.body as string)).toEqual({ reason: "" });
  });

  it("resolves a paused-run checkpoint against the checkpoints endpoint", async () => {
    let checkpoints = [CHECKPOINT];
    const posts: Array<[string, RequestInit]> = [];
    installFetch({
      checkpoints: () => checkpoints,
      onPost: (url, init) => {
        posts.push([url, init]);
        checkpoints = [];
        return json({
          id: CHECKPOINT.id,
          run_id: CHECKPOINT.run_id,
          status: "resolved",
          resolution: "yes, include them",
          resolved_at: "2026-05-23T12:00:00Z",
          run_status: "open",
        });
      },
    });

    render(<Decisions />);
    // Answer the blocking question, then submit.
    await userEvent.type(await screen.findByLabelText(/Your answer/i), "yes, include them");
    await userEvent.click(screen.getByRole("button", { name: /Answer|Resolve|Send/ }));

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /Pending/ })).toHaveTextContent("0");
    });
    const [url, init] = posts[0];
    expect(url).toBe(`/api/v1/checkpoints/${CHECKPOINT.id}/resolve`);
    expect(JSON.parse(init.body as string)).toEqual({ answer: "yes, include them" });
  });

  it("lists pending proposals in plain language", async () => {
    installFetch({ proposals: () => [PROPOSAL] });
    render(<Decisions />);

    await waitFor(() => {
      expect(screen.getByText(/merge concepts/)).toBeInTheDocument();
    });
  });

  it("filters the visible list with the search box", async () => {
    installFetch({ proposals: () => [PROPOSAL, PROPOSAL_2] });
    render(<Decisions />);

    await screen.findByText(/merge concepts/);
    const search = screen.getByRole("searchbox");
    await userEvent.type(search, "jwt");

    expect(screen.getByText(/create concept/)).toBeInTheDocument();
    expect(screen.queryByText(/merge concepts/)).not.toBeInTheDocument();
  });

  it("shows the Resolved audit trail with its recorded outcome", async () => {
    installFetch({ proposals: () => [], decisions: () => [DECISION] });
    render(<Decisions />);

    await userEvent.click(await screen.findByRole("tab", { name: /Resolved/ }));
    // The recorded decision kind surfaces as the outcome.
    expect(screen.getByText(/must.link/i)).toBeInTheDocument();
  });

  it("folds resolved deliveries + answered checkpoints into the Resolved tab", async () => {
    installFetch({
      decisions: () => [DECISION],
      resolvedSafemode: () => [RESOLVED_DELIVERY],
      resolvedCheckpoints: () => [RESOLVED_CHECKPOINT],
    });
    render(<Decisions />);

    const tablist = await screen.findByRole("tablist");
    // 1 canon decision + 1 resolved delivery + 1 answered checkpoint = 3.
    expect(within(tablist).getByRole("tab", { name: /Resolved/ })).toHaveTextContent("3");

    await userEvent.click(within(tablist).getByRole("tab", { name: /Resolved/ }));
    // Delivery outcome, the canon decision, and the answered checkpoint (with
    // its question + recorded answer) all show as history.
    expect(screen.getByText("Delivery approved")).toBeInTheDocument();
    expect(screen.getByText(/must.link/i)).toBeInTheDocument();
    expect(screen.getByText(/Ship to staging first\?/)).toBeInTheDocument();
    expect(screen.getByText(/yes, staging then prod/)).toBeInTheDocument();
  });

  it("titles a RESOLVED delivery with its task + links to the proof (#7 — not blind history)", async () => {
    // Mirror of the pending-delivery review-context join, on the Resolved tab:
    // the resolved row leads with the joined task title + product chip + a
    // "View report" link instead of a blind generic "Delivery approved".
    installFetch({
      resolvedSafemode: () => [RESOLVED_DELIVERY], // deliverable_id "del-9"
      deliverables: () =>
        [
          {
            id: "del-9",
            run_id: "run-42",
            workspace_id: "ws-1",
            deliverable_type: "pr",
            summary: "Add the CSV export endpoint with pagination.",
            artifact_refs: [],
            artifact_uri: null,
            verified: true,
            created_at: "2026-05-24T08:00:00Z",
          },
        ] as Deliverable[],
      runs: () =>
        [
          {
            id: "run-42",
            workspace_id: "ws-1",
            product_id: "prod-9",
            request_id: null,
            status: "shipped",
            intent: "Add a CSV export",
            created_at: "2026-05-24T08:00:00Z",
            updated_at: "2026-05-24T09:00:00Z",
          },
        ] as Run[],
      products: () =>
        [
          {
            id: "prod-9",
            workspace_id: "ws-1",
            name: "acme-corp",
            slug: "acme-corp",
            repo_url: null,
            created_at: "2026-05-24T08:00:00Z",
            updated_at: "2026-05-24T08:00:00Z",
          },
        ] as Product[],
    });

    render(<Decisions />);
    await userEvent.click(await screen.findByRole("tab", { name: /Resolved/ }));

    // Concise title (the deliverable's summary), the product chip, and a proof
    // link — the same context the PENDING delivery row carries.
    expect(screen.getByText("Add the CSV export endpoint with pagination.")).toBeInTheDocument();
    expect(screen.getByText("acme-corp")).toBeInTheDocument();
    const proof = screen.getByRole("link", { name: /View report/ });
    expect(proof).toHaveAttribute("href", "/deliverables/del-9");
    // The outcome ("Delivery approved") is still present as the subtitle.
    expect(screen.getByText("Delivery approved")).toBeInTheDocument();
  });

  it("opens a detail/resolve panel for a pending item and Accepts it", async () => {
    let proposals = [PROPOSAL];
    const posts: Array<[string, RequestInit]> = [];
    installFetch({
      proposals: () => proposals,
      onPost: (url, init) => {
        posts.push([url, init]);
        proposals = [];
        return json({ proposal_path: PROPOSAL.id, status: "accepted", results: [] });
      },
    });

    render(<Decisions />);
    await userEvent.click(await screen.findByRole("button", { name: /merge concepts/ }));

    // Detail panel reveals the affected path + the resolve affordances.
    const panel = screen.getByRole("dialog");
    expect(within(panel).getByText(PROPOSAL.action_path)).toBeInTheDocument();
    await userEvent.click(within(panel).getByRole("button", { name: "Accept" }));

    await waitFor(() => {
      // Resolved → leaves the pending list (count → 0).
      expect(screen.getByRole("tab", { name: /Pending/ })).toHaveTextContent("0");
    });
    const [url, init] = posts[0];
    expect(url).toBe(`/api/v1/decisions/${encodeURIComponent(PROPOSAL.id)}/accept`);
    expect(init.method).toBe("POST");
  });

  it("Rejects a pending item with an optional reason", async () => {
    let proposals = [PROPOSAL];
    const posts: Array<[string, RequestInit]> = [];
    installFetch({
      proposals: () => proposals,
      onPost: (url, init) => {
        posts.push([url, init]);
        proposals = [];
        return json({ proposal_path: PROPOSAL.id, status: "rejected", reason: "not a dup" });
      },
    });

    render(<Decisions />);
    await userEvent.click(await screen.findByRole("button", { name: /merge concepts/ }));

    const panel = screen.getByRole("dialog");
    await userEvent.type(within(panel).getByRole("textbox"), "not a dup");
    await userEvent.click(within(panel).getByRole("button", { name: "Reject" }));

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /Pending/ })).toHaveTextContent("0");
    });
    const [url, init] = posts[0];
    expect(url).toBe(`/api/v1/decisions/${encodeURIComponent(PROPOSAL.id)}/reject`);
    expect(JSON.parse(init.body as string)).toEqual({ reason: "not a dup" });
  });

  it("shows a calm inline error on a failed accept — panel stays actionable", async () => {
    installFetch({
      proposals: () => [PROPOSAL],
      onPost: () => json("boom", 500),
    });

    render(<Decisions />);
    await userEvent.click(await screen.findByRole("button", { name: /merge concepts/ }));
    const panel = screen.getByRole("dialog");
    await userEvent.click(within(panel).getByRole("button", { name: "Accept" }));

    await waitFor(() => {
      expect(screen.getByText("Couldn’t do that. Please try again.")).toBeInTheDocument();
    });
    expect(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Accept" }),
    ).toBeEnabled();
  });

  it("shows the calm empty state when nothing is pending across all kinds", async () => {
    installFetch({});
    render(<Decisions />);

    await waitFor(() => {
      expect(screen.getByText("Nothing needs you right now.")).toBeInTheDocument();
    });
  });

  it("syncs the nav pending-count store with the FULL pending size (all kinds)", async () => {
    installFetch({
      proposals: () => [PROPOSAL, PROPOSAL_2],
      safemode: () => [SAFEMODE],
      checkpoints: () => [CHECKPOINT],
      decisions: () => [DECISION],
    });

    function Probe() {
      return <span data-testid="badge">{usePendingDecisionsCount()}</span>;
    }
    render(
      <>
        <Decisions />
        <Probe />
      </>,
    );

    await waitFor(() => {
      // 2 proposals + 1 delivery + 1 checkpoint = 4. Resolved items (the log)
      // do NOT inflate the badge.
      expect(screen.getByTestId("badge")).toHaveTextContent("4");
    });
  });
});
