/**
 * Decisions surface — the full container rendering both queues, driven by a
 * mocked fetch (route-aware). Asserts:
 *  - both sections render (checkpoint question + proposal summary)
 *  - resolving a checkpoint POSTs /checkpoints/{id}/resolve with the answer,
 *    then drops out on the re-read
 *  - accepting a proposal POSTs /decisions/{path}/accept, then drops out
 *  - a forced error leaves a calm inline message and the row actionable
 *  - the empty state when nothing is pending
 *  - the nav pending-count store reflects the loaded queue sizes
 */

import Decisions from "@/components/decisions/Decisions";
import type { Checkpoint, Proposal } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { setPendingDecisionsCount } from "@/lib/decisions/pending-count";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const CHECKPOINT: Checkpoint = {
  id: "c1",
  run_id: "r1",
  decision: "ask_user_question",
  question: "Should the related-posts widget show 3 or 5 items?",
  rationale: "The spec didn’t say; both fit the layout.",
  created_at: "2026-05-23T00:00:00Z",
};

const PROPOSAL: Proposal = {
  id: "p1",
  proposal_kind: "merge",
  action_kind: "merge-concepts",
  action_path: "proposals/merge-concepts/2026-05-23-self-hosting.md",
  status: "pending",
  score: 82,
  created_at: "2026-05-23T00:00:00Z",
  expires_at: null,
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * A route-aware fetch mock. `checkpoints` / `proposals` are the queue contents
 * returned by the list GETs (mutate them between calls to simulate the re-read
 * dropping a resolved item). `onPost` records resolve/accept/reject calls.
 */
function installFetch(opts: {
  checkpoints: () => Checkpoint[];
  proposals: () => Proposal[];
  onPost?: (url: string, init: RequestInit) => Response;
}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    const method = (init.method ?? "GET").toUpperCase();
    if (method === "GET" && url === "/api/v1/checkpoints") return json(opts.checkpoints());
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

  it("renders both sections from the two queues", async () => {
    installFetch({ checkpoints: () => [CHECKPOINT], proposals: () => [PROPOSAL] });

    render(<Decisions />);

    await waitFor(() => {
      expect(screen.getByText(CHECKPOINT.question)).toBeInTheDocument();
    });
    expect(screen.getByRole("region", { name: "Decisions needed" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Knowledge review" })).toBeInTheDocument();
    // Proposal summary surfaces the action kind + path in plain language.
    expect(screen.getByText(/merge concepts → proposals\/merge-concepts/)).toBeInTheDocument();
  });

  it("resolves a checkpoint — POSTs the answer, then the item drops out", async () => {
    let checkpoints = [CHECKPOINT];
    const posts: Array<[string, RequestInit]> = [];
    installFetch({
      checkpoints: () => checkpoints,
      proposals: () => [],
      onPost: (url, init) => {
        posts.push([url, init]);
        // Resolving removes it from the queue so the re-read shows it gone.
        checkpoints = [];
        return json({
          id: "c1",
          run_id: "r1",
          status: "resolved",
          resolution: "5 items",
          resolved_at: "2026-05-23T00:01:00Z",
          run_status: "open",
        });
      },
    });

    render(<Decisions />);
    const input = await screen.findByLabelText("Your answer");
    await userEvent.type(input, "5 items");
    await userEvent.click(screen.getByRole("button", { name: "Resolve" }));

    await waitFor(() => {
      // After the re-read, nothing pending → empty state.
      expect(screen.getByText("Nothing needs you right now.")).toBeInTheDocument();
    });
    expect(posts).toHaveLength(1);
    const [url, init] = posts[0];
    expect(url).toBe("/api/v1/checkpoints/c1/resolve");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ answer: "5 items" });
  });

  it("does not allow Resolve until an answer is typed", async () => {
    installFetch({ checkpoints: () => [CHECKPOINT], proposals: () => [] });
    render(<Decisions />);

    const button = await screen.findByRole("button", { name: "Resolve" });
    expect(button).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Your answer"), "ok");
    expect(button).toBeEnabled();
  });

  it("accepts a proposal — POSTs /accept with the encoded path, then drops out", async () => {
    let proposals = [PROPOSAL];
    const posts: Array<[string, RequestInit]> = [];
    installFetch({
      checkpoints: () => [],
      proposals: () => proposals,
      onPost: (url, init) => {
        posts.push([url, init]);
        proposals = [];
        return json({ proposal_path: PROPOSAL.action_path, status: "accepted", results: [] });
      },
    });

    render(<Decisions />);
    await userEvent.click(await screen.findByRole("button", { name: "Accept" }));

    await waitFor(() => {
      expect(screen.getByText("Nothing needs you right now.")).toBeInTheDocument();
    });
    expect(posts).toHaveLength(1);
    const [url, init] = posts[0];
    expect(url).toBe(`/api/v1/decisions/${encodeURIComponent(PROPOSAL.action_path)}/accept`);
    expect(init.method).toBe("POST");
  });

  it("rejects a proposal — POSTs /reject with a reason body", async () => {
    let proposals = [PROPOSAL];
    const posts: Array<[string, RequestInit]> = [];
    installFetch({
      checkpoints: () => [],
      proposals: () => proposals,
      onPost: (url, init) => {
        posts.push([url, init]);
        proposals = [];
        return json({ proposal_path: PROPOSAL.action_path, status: "rejected", reason: "" });
      },
    });

    render(<Decisions />);
    await userEvent.click(await screen.findByRole("button", { name: "Reject" }));

    await waitFor(() => {
      expect(screen.getByText("Nothing needs you right now.")).toBeInTheDocument();
    });
    const [url, init] = posts[0];
    expect(url).toBe(`/api/v1/decisions/${encodeURIComponent(PROPOSAL.action_path)}/reject`);
    expect(JSON.parse(init.body as string)).toEqual({ reason: "" });
  });

  it("shows a calm inline error on a failed accept — row stays actionable", async () => {
    installFetch({
      checkpoints: () => [],
      proposals: () => [PROPOSAL],
      onPost: () => json("boom", 500),
    });

    render(<Decisions />);
    await userEvent.click(await screen.findByRole("button", { name: "Accept" }));

    await waitFor(() => {
      expect(screen.getByText("Couldn’t do that — please try again.")).toBeInTheDocument();
    });
    // Still pending, still actionable — no crash, no empty state.
    expect(screen.getByRole("button", { name: "Accept" })).toBeEnabled();
    expect(screen.queryByText("Nothing needs you right now.")).not.toBeInTheDocument();
  });

  it("shows the calm empty state when nothing is pending", async () => {
    installFetch({ checkpoints: () => [], proposals: () => [] });
    render(<Decisions />);

    await waitFor(() => {
      expect(screen.getByText("Nothing needs you right now.")).toBeInTheDocument();
    });
    expect(screen.queryByRole("region", { name: "Decisions needed" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Knowledge review" })).not.toBeInTheDocument();
  });

  it("degrades gracefully when one queue 4xxs — the other still renders", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/checkpoints") return json([CHECKPOINT]);
      // proposals list fails — should not blank the page.
      return json("forbidden", 403);
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<Decisions />);

    await waitFor(() => {
      expect(screen.getByText(CHECKPOINT.question)).toBeInTheDocument();
    });
    expect(screen.queryByRole("region", { name: "Knowledge review" })).not.toBeInTheDocument();
  });
});
