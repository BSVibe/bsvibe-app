/**
 * Decisions surface — the redesigned inbox (Stitch screens
 * 1175801d… Inbox + 5bf54bdf… detail). Driven by a route-aware mocked fetch.
 *
 * The surface is the canonicalization proposals queue:
 *  - Pending tab   ← GET /api/v1/decisions?status_filter=pending
 *  - Resolved tab  ← GET /api/v1/decisions/log  (the audit trail)
 *  - tab labels carry live counts
 *  - a client-side search box filters the visible list
 *  - opening a pending item shows a calm detail/resolve panel (kind + affected
 *    path + Accept / Reject, Reject taking an optional reason)
 *  - Accept POSTs /accept with the encoded vault path; Reject POSTs /reject
 *    with a { reason } body; after resolve the item leaves Pending
 *  - a forced error keeps the detail panel actionable with a calm message
 *  - the empty Pending state stays calm
 *  - the nav pending-count store reflects the pending queue size
 */

import Decisions from "@/components/decisions/Decisions";
import type { DecisionLogEntry, Proposal } from "@/lib/api/types";
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

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * A route-aware fetch mock. `proposals` / `decisions` are the queue contents
 * returned by the list GETs (mutate them between calls to simulate the re-read
 * dropping a resolved item). `onPost` records resolve calls.
 */
function installFetch(opts: {
  proposals: () => Proposal[];
  decisions?: () => DecisionLogEntry[];
  onPost?: (url: string, init: RequestInit) => Response;
}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    const method = (init.method ?? "GET").toUpperCase();
    if (method === "GET" && url.startsWith("/api/v1/decisions/log")) {
      return json((opts.decisions ?? (() => []))());
    }
    if (method === "GET" && url.startsWith("/api/v1/decisions?")) return json(opts.proposals());
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
      expect(screen.getByText("Couldn’t do that — please try again.")).toBeInTheDocument();
    });
    expect(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Accept" }),
    ).toBeEnabled();
  });

  it("shows the calm empty state when nothing is pending", async () => {
    installFetch({ proposals: () => [] });
    render(<Decisions />);

    await waitFor(() => {
      expect(screen.getByText("Nothing needs you right now.")).toBeInTheDocument();
    });
  });

  it("syncs the nav pending-count store with the pending queue size", async () => {
    installFetch({ proposals: () => [PROPOSAL, PROPOSAL_2], decisions: () => [DECISION] });

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
      // Resolved items do NOT inflate the nav badge — only pending.
      expect(screen.getByTestId("badge")).toHaveTextContent("2");
    });
  });
});
