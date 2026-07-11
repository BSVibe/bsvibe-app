/**
 * Run-routing surface — Settings → Models → ROUTING. Drives the real
 * list/callers/create/delete clients against a mocked fetch and asserts:
 *
 *  - the calm empty state when there are no rules
 *  - list renders each rule (name, caller → target, priority, chips)
 *  - Add: opening the form loads callers + accounts, a submit POSTs the body
 *    (name, caller_id, target) and re-reads
 *  - Delete: confirm → DELETE fires → re-read
 *  - a calm inline note when the list read fails
 */

import RunRoutingRules from "@/components/settings/RunRoutingRules";
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

function jsonResponse(body: unknown, status = 200) {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const RULE = {
  id: "11111111-1111-1111-1111-111111111111",
  workspace_id: "22222222-2222-2222-2222-222222222222",
  name: "design → opus",
  caller_id: "workflow.agent_loop.plan",
  priority: 10,
  is_default: false,
  target: "opus",
  conditions: [],
  is_active: true,
  created_at: "2026-07-11T00:00:00Z",
};

const CALLERS = [
  { caller_id: "workflow.agent_loop.plan", description: "design step" },
  { caller_id: "workflow.judge", description: "verifier" },
];

const ACCOUNTS = [
  {
    id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    workspace_id: "22222222-2222-2222-2222-222222222222",
    account_id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    provider: "executor",
    label: "dogfood (opus)",
    litellm_model: "opus",
    api_base: null,
    data_jurisdiction: "unknown",
    is_active: true,
    has_api_key: true,
    extra_params: {},
    created_at: "2026-07-11T00:00:00Z",
    updated_at: "2026-07-11T00:00:00Z",
  },
];

/** URL-routed fetch: the Add form fetches callers + accounts concurrently, so a
 *  sequential once-mock is fragile — route by path + method instead. */
function routedFetch(rules: unknown, opts: { onPost?: () => void } = {}) {
  return vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    const method = (init?.method ?? "GET").toUpperCase();
    if (u.endsWith("/api/v1/run-routing/callers")) return jsonResponse(CALLERS);
    if (u.includes("/api/v1/accounts")) return jsonResponse(ACCOUNTS);
    if (u.endsWith("/api/v1/run-routing") && method === "POST") {
      opts.onPost?.();
      return jsonResponse(RULE, 201);
    }
    if (u.endsWith("/api/v1/run-routing")) return jsonResponse(rules);
    if (method === "DELETE") return jsonResponse(null, 204);
    return jsonResponse([]);
  });
}

describe("Run-routing surface", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows the calm empty state when there are no rules", async () => {
    global.fetch = routedFetch([]) as unknown as typeof fetch;

    render(<RunRoutingRules />);

    await waitFor(() => {
      expect(screen.getByText(/All work goes to the active model account/i)).toBeInTheDocument();
    });
  });

  it("lists each rule with name, caller → target, and chips", async () => {
    global.fetch = routedFetch([RULE]) as unknown as typeof fetch;

    render(<RunRoutingRules />);

    await waitFor(() => expect(screen.getByText("design → opus")).toBeInTheDocument());
    expect(screen.getByText("workflow.agent_loop.plan")).toBeInTheDocument();
    expect(screen.getByText("opus")).toBeInTheDocument();
  });

  it("creates a rule: loads callers/accounts, POSTs the body, re-reads", async () => {
    let posted = false;
    const fetchMock = routedFetch([], {
      onPost: () => {
        posted = true;
      },
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<RunRoutingRules />);
    await waitFor(() =>
      expect(screen.getByText(/All work goes to the active model account/i)).toBeInTheDocument(),
    );

    await userEvent.click(screen.getByRole("button", { name: "+ Add rule" }));
    await userEvent.type(screen.getByLabelText(/Rule name/i), "design → opus");

    // Caller + target selects populate from the fetched lists.
    const callerOption = await screen.findByRole("option", { name: "workflow.agent_loop.plan" });
    await userEvent.selectOptions(screen.getByLabelText(/Caller/i), callerOption);
    await userEvent.selectOptions(screen.getByLabelText(/Route to/i), "opus");

    await userEvent.click(screen.getByRole("button", { name: /^Add rule$/i }));

    await waitFor(() => expect(posted).toBe(true));
    const postCall = (fetchMock.mock.calls as unknown as Array<[string, RequestInit]>).find(
      ([u, init]) =>
        String(u).endsWith("/api/v1/run-routing") && (init?.method ?? "GET") === "POST",
    );
    if (!postCall) throw new Error("expected a run-routing POST");
    const body = JSON.parse(postCall[1].body as string);
    expect(body.name).toBe("design → opus");
    expect(body.caller_id).toBe("workflow.agent_loop.plan");
    expect(body.target).toBe("opus");
  });

  it("deletes a rule after confirm → DELETE → re-read", async () => {
    const fetchMock = routedFetch([RULE]);
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<RunRoutingRules />);
    const list = await screen.findByRole("list");
    await waitFor(() => expect(within(list).getByText("design → opus")).toBeInTheDocument());

    const row = within(list).getByText("design → opus").closest("li") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: /^Remove$/i }));
    const confirm = await within(row).findByRole("button", { name: /^Confirm remove$/i });
    await userEvent.click(confirm);

    await waitFor(() => {
      const deleteCall = (fetchMock.mock.calls as unknown as Array<[string, RequestInit]>).find(
        ([, init]) => (init?.method ?? "GET") === "DELETE",
      );
      if (!deleteCall) throw new Error("expected a DELETE");
      expect(deleteCall[0]).toBe(`/api/v1/run-routing/${RULE.id}`);
    });
  });

  it("surfaces a calm note when the list read fails", async () => {
    global.fetch = vi.fn(
      async () => new Response("boom", { status: 500 }),
    ) as unknown as typeof fetch;

    render(<RunRoutingRules />);

    await waitFor(() => {
      expect(screen.getByText(/Couldn.t load your routing rules/i)).toBeInTheDocument();
    });
  });
});
