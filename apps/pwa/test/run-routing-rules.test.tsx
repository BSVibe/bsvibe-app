/**
 * Run-routing surface — Settings → Models → ROUTING (Lift 6 refined). Drives the
 * real list/callers/create/update/delete/compile clients against a mocked fetch:
 *
 *  - calm empty state; is_default rules hidden (the default is the picker above)
 *  - each rule is one line `caller → friendly model` (target resolved via accounts)
 *  - Add: caller + target selects only (no name / priority) → POST
 *  - Edit: inline form → PATCH
 *  - Delete: confirm → DELETE
 *  - NL: draft → preview → apply (a default proposal sets the workspace default)
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

const DEFAULT_RULE = {
  ...RULE,
  id: "99999999-9999-9999-9999-999999999999",
  is_default: true,
  caller_id: null,
};

const CALLERS = [
  { caller_id: "workflow.agent_loop.plan", description: "design step" },
  { caller_id: "workflow.judge", description: "verifier" },
];

function acct(id: string, label: string, litellm_model: string) {
  return {
    id,
    workspace_id: "22222222-2222-2222-2222-222222222222",
    account_id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    provider: "executor",
    label,
    litellm_model,
    api_base: null,
    data_jurisdiction: "unknown",
    is_active: true,
    has_api_key: true,
    extra_params: {},
    created_at: "2026-07-11T00:00:00Z",
    updated_at: "2026-07-11T00:00:00Z",
  };
}
const ACCOUNTS = [
  acct("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "dogfood (opus)", "opus"),
  acct("cccccccc-cccc-cccc-cccc-cccccccccccc", "dogfood (sonnet)", "sonnet"),
];

function routedFetch(rules: unknown, hooks: Record<string, () => void> = {}) {
  return vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    const method = (init?.method ?? "GET").toUpperCase();
    if (u.endsWith("/api/v1/run-routing/callers")) return jsonResponse(CALLERS);
    if (u.includes("/api/v1/accounts")) return jsonResponse(ACCOUNTS);
    if (u.endsWith("/api/v1/run-routing/compile")) return jsonResponse({ proposals: [] });
    if (u.endsWith("/api/v1/run-routing") && method === "POST") {
      hooks.onPost?.();
      return jsonResponse(RULE, 201);
    }
    if (u.match(/\/api\/v1\/run-routing\/[^/]+$/) && method === "PATCH") {
      hooks.onPatch?.();
      return jsonResponse({ ...RULE, target: "sonnet", caller_id: "workflow.judge" });
    }
    if (u.match(/\/api\/v1\/run-routing\/[^/]+$/) && method === "DELETE") {
      hooks.onDelete?.();
      return jsonResponse(null, 204);
    }
    if (u.endsWith("/api/v1/run-routing")) return jsonResponse(rules);
    return jsonResponse([]);
  });
}

describe("Run-routing surface (Lift 6)", () => {
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
    await waitFor(() =>
      expect(screen.getByText(/All work goes to the active model account/i)).toBeInTheDocument(),
    );
  });

  it("renders one line caller → friendly model; no priority chip", async () => {
    global.fetch = routedFetch([RULE]) as unknown as typeof fetch;
    render(<RunRoutingRules />);
    // The caller shows a localized human label, NOT the raw technical id.
    await waitFor(() => expect(screen.getByText("Design & planning")).toBeInTheDocument());
    expect(screen.queryByText("workflow.agent_loop.plan")).not.toBeInTheDocument();
    // Target resolves to the friendly account label, not the raw litellm id.
    expect(screen.getByText("dogfood (opus)")).toBeInTheDocument();
    // The rule's freeform name is NOT shown (no duplication) and no priority chip.
    expect(screen.queryByText("design → opus")).not.toBeInTheDocument();
    expect(screen.queryByText(/Priority \d/i)).not.toBeInTheDocument();
  });

  it("hides is_default rules (the default is the picker above)", async () => {
    global.fetch = routedFetch([DEFAULT_RULE]) as unknown as typeof fetch;
    render(<RunRoutingRules />);
    await waitFor(() =>
      expect(screen.getByText(/All work goes to the active model account/i)).toBeInTheDocument(),
    );
  });

  it("creates a rule from caller + target selects (no name/priority) → POST", async () => {
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
    // Options show the localized label; the value is still the caller_id.
    await screen.findByRole("option", { name: "Design & planning" });
    await userEvent.selectOptions(screen.getByLabelText(/Caller/i), "workflow.agent_loop.plan");
    await userEvent.selectOptions(screen.getByLabelText(/Route to/i), "opus");
    await userEvent.click(screen.getByRole("button", { name: /^Add rule$/i }));

    await waitFor(() => expect(posted).toBe(true));
    const call = (fetchMock.mock.calls as unknown as Array<[string, RequestInit]>).find(
      ([u, i]) => String(u).endsWith("/api/v1/run-routing") && (i?.method ?? "GET") === "POST",
    );
    if (!call) throw new Error("expected POST");
    const body = JSON.parse(call[1].body as string);
    expect(body.caller_id).toBe("workflow.agent_loop.plan");
    expect(body.target).toBe("opus");
    // Name auto-derived; no priority surfaced.
    expect(body.name).toBe("workflow.agent_loop.plan → opus");
  });

  it("edits a rule inline → PATCH", async () => {
    let patched = false;
    const fetchMock = routedFetch([RULE], {
      onPatch: () => {
        patched = true;
      },
    });
    global.fetch = fetchMock as unknown as typeof fetch;
    render(<RunRoutingRules />);
    const list = await screen.findByRole("list");
    await waitFor(() => expect(within(list).getByText("Design & planning")).toBeInTheDocument());

    await userEvent.click(within(list).getByRole("button", { name: /^Edit$/i }));
    // The edit form is prefilled; change the target then save.
    await userEvent.selectOptions(screen.getByLabelText(/Route to/i), "sonnet");
    await userEvent.click(screen.getByRole("button", { name: /^Save$/i }));

    await waitFor(() => expect(patched).toBe(true));
    const call = (fetchMock.mock.calls as unknown as Array<[string, RequestInit]>).find(
      ([, i]) => (i?.method ?? "GET") === "PATCH",
    );
    if (!call) throw new Error("expected PATCH");
    expect(JSON.parse(call[1].body as string).target).toBe("sonnet");
  });

  it("deletes a rule after confirm → DELETE", async () => {
    let deleted = false;
    const fetchMock = routedFetch([RULE], {
      onDelete: () => {
        deleted = true;
      },
    });
    global.fetch = fetchMock as unknown as typeof fetch;
    render(<RunRoutingRules />);
    const list = await screen.findByRole("list");
    await waitFor(() => expect(within(list).getByText("Design & planning")).toBeInTheDocument());

    await userEvent.click(within(list).getByRole("button", { name: /^Remove$/i }));
    await userEvent.click(await screen.findByRole("button", { name: /^Confirm remove$/i }));
    await waitFor(() => expect(deleted).toBe(true));
  });

  it("NL: a default proposal sets the workspace default, a caller proposal creates a rule", async () => {
    let putDefault = false;
    let created = false;
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      const method = (init?.method ?? "GET").toUpperCase();
      if (u.endsWith("/api/v1/run-routing/callers")) return jsonResponse(CALLERS);
      if (u.includes("/api/v1/accounts")) return jsonResponse(ACCOUNTS);
      if (u.endsWith("/api/v1/run-routing/compile"))
        return jsonResponse({
          proposals: [
            { name: "d", caller_id: null, target: "sonnet", priority: 100, is_default: true },
            {
              name: "p",
              caller_id: "workflow.agent_loop.plan",
              target: "opus",
              priority: 10,
              is_default: false,
            },
          ],
        });
      if (u.includes("/api/v1/workspace") && method === "PATCH") {
        putDefault = true;
        return jsonResponse({});
      }
      if (u.endsWith("/api/v1/run-routing") && method === "POST") {
        created = true;
        return jsonResponse(RULE, 201);
      }
      return jsonResponse([]);
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<RunRoutingRules />);
    await waitFor(() =>
      expect(screen.getByText(/All work goes to the active model account/i)).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByRole("button", { name: /Describe in words/i }));
    await userEvent.type(screen.getByPlaceholderText(/Design work goes to opus/i), "설계는 opus");
    await userEvent.click(screen.getByRole("button", { name: /^Draft rules$/i }));

    await waitFor(() => expect(screen.getByText("Design & planning")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /^Apply all$/i }));

    await waitFor(() => expect(putDefault).toBe(true));
    expect(created).toBe(true);
  });

  it("surfaces a calm note when the list read fails", async () => {
    global.fetch = vi.fn(
      async () => new Response("boom", { status: 500 }),
    ) as unknown as typeof fetch;
    render(<RunRoutingRules />);
    await waitFor(() =>
      expect(screen.getByText(/Couldn.t load your routing rules/i)).toBeInTheDocument(),
    );
  });
});
