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

// An N3 non-stage rule: caller_id null + a category condition (classified_intent).
const CATEGORY_RULE = {
  ...RULE,
  id: "33333333-3333-3333-3333-333333333333",
  name: "marketing → opus",
  caller_id: null,
  conditions: [{ field: "classified_intent", operator: "eq", value: "marketing", negate: false }],
};

// A complexity condition rule (caller_id null + estimated_tokens > N).
const COMPLEXITY_RULE = {
  ...RULE,
  id: "44444444-4444-4444-4444-444444444444",
  name: "complex → opus",
  caller_id: null,
  conditions: [{ field: "estimated_tokens", operator: "gt", value: 8000, negate: false }],
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

  it("renders a condition-based rule's match in human terms in the LIST (not blank)", async () => {
    global.fetch = routedFetch([CATEGORY_RULE, COMPLEXITY_RULE]) as unknown as typeof fetch;
    render(<RunRoutingRules />);
    const list = await screen.findByRole("list", { name: /Routing rules/i });
    // Category rule: caller_id is null but the left side is NOT blank — it reads
    // "<value> (category)", never an empty span before the arrow.
    await waitFor(() =>
      expect(within(list).getByText(/marketing \(category\)/i)).toBeInTheDocument(),
    );
    // Complexity rule: "<field> <op> <value>".
    expect(within(list).getByText(/estimated_tokens > 8000/i)).toBeInTheDocument();
    // Both still resolve their target to the friendly account label.
    expect(within(list).getAllByText("dogfood (opus)").length).toBe(2);
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

  it("NL is the primary surface — the textarea shows without any toggle", async () => {
    global.fetch = routedFetch([]) as unknown as typeof fetch;
    render(<RunRoutingRules />);
    // No "Describe in words" toggle to click — the NL textarea is right there.
    expect(await screen.findByPlaceholderText(/Design work goes to opus/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Describe in words/i })).not.toBeInTheDocument();
  });

  it("NL: rich preview renders each dimension in human terms, Apply → one POST /compile/apply", async () => {
    let applyBody: unknown = null;
    let applyCalls = 0;
    let perRuleCreate = 0;
    let defaultPatch = 0;
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      const method = (init?.method ?? "GET").toUpperCase();
      if (u.endsWith("/api/v1/run-routing/callers")) return jsonResponse(CALLERS);
      if (u.includes("/api/v1/accounts")) return jsonResponse(ACCOUNTS);
      if (u.endsWith("/api/v1/run-routing/compile") && method === "POST")
        return jsonResponse({
          proposals: [
            {
              name: "marketing",
              target: "sonnet",
              is_default: false,
              priority: 10,
              caller_id: null,
              condition: { field: "classified_intent", operator: "eq", value: "marketing" },
              intent_name: "marketing",
              intent_examples: ["draft ad copy", "write a launch post"],
            },
            {
              name: "complex",
              target: "opus",
              is_default: false,
              priority: 10,
              caller_id: null,
              condition: { field: "estimated_tokens", operator: "gt", value: 8000 },
              intent_name: null,
              intent_examples: null,
            },
            {
              name: "korean",
              target: "sonnet",
              is_default: false,
              priority: 10,
              caller_id: null,
              condition: { field: "detected_language", operator: "eq", value: "ko" },
              intent_name: null,
              intent_examples: null,
            },
            {
              name: "design stage",
              target: "opus",
              is_default: false,
              priority: 10,
              caller_id: "workflow.agent_loop.plan",
              condition: null,
              intent_name: null,
              intent_examples: null,
            },
            {
              name: "the rest",
              target: "sonnet",
              is_default: true,
              priority: 100,
              caller_id: null,
              condition: null,
              intent_name: null,
              intent_examples: null,
            },
          ],
        });
      if (u.endsWith("/api/v1/run-routing/compile/apply") && method === "POST") {
        applyCalls += 1;
        applyBody = JSON.parse(init?.body as string);
        return jsonResponse({ created: [RULE], default_set: true }, 201);
      }
      // These MUST NOT be hit — apply is one backend call now.
      if (u.endsWith("/api/v1/run-routing") && method === "POST") perRuleCreate += 1;
      if (u.includes("/api/v1/workspace") && method === "PATCH") defaultPatch += 1;
      return jsonResponse([]);
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<RunRoutingRules />);
    await userEvent.type(
      await screen.findByPlaceholderText(/Design work goes to opus/i),
      "마케팅은 sonnet, 복잡한 건 opus",
    );
    await userEvent.click(screen.getByRole("button", { name: /^Draft rules$/i }));

    // Category: "<intent> (category) → <model>".
    await waitFor(() => expect(screen.getByText(/marketing \(category\)/i)).toBeInTheDocument());
    // Complexity: the field + operator + value are shown in human terms.
    expect(screen.getByText(/estimated_tokens > 8000/i)).toBeInTheDocument();
    // Language dimension.
    expect(screen.getByText(/detected_language = ko/i)).toBeInTheDocument();
    // Stage via callerDisplay (localized).
    expect(screen.getByText("Design & planning")).toBeInTheDocument();
    // Default proposal reads as "Default model → <model>".
    const proposed = screen.getByRole("list", { name: /Proposed rules/i });
    expect(within(proposed).getByText(/Default model/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /^Apply all$/i }));

    await waitFor(() => expect(applyCalls).toBe(1));
    // Exactly one backend apply call — no per-proposal client loop.
    expect(perRuleCreate).toBe(0);
    expect(defaultPatch).toBe(0);
    const body = applyBody as { proposals: unknown[] };
    expect(body.proposals).toHaveLength(5);
  });

  it("NL: a calm error shows when apply fails", async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      const method = (init?.method ?? "GET").toUpperCase();
      if (u.endsWith("/api/v1/run-routing/callers")) return jsonResponse(CALLERS);
      if (u.includes("/api/v1/accounts")) return jsonResponse(ACCOUNTS);
      if (u.endsWith("/api/v1/run-routing/compile") && method === "POST")
        return jsonResponse({
          proposals: [
            {
              name: "the rest",
              target: "sonnet",
              is_default: true,
              priority: 100,
              caller_id: null,
              condition: null,
              intent_name: null,
              intent_examples: null,
            },
          ],
        });
      if (u.endsWith("/api/v1/run-routing/compile/apply") && method === "POST")
        return new Response("boom", { status: 500 });
      return jsonResponse([]);
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<RunRoutingRules />);
    await userEvent.type(
      await screen.findByPlaceholderText(/Design work goes to opus/i),
      "나머지는 sonnet",
    );
    await userEvent.click(screen.getByRole("button", { name: /^Draft rules$/i }));
    await userEvent.click(await screen.findByRole("button", { name: /^Apply all$/i }));

    await waitFor(() =>
      expect(screen.getByText(/Couldn.t apply those rules/i)).toBeInTheDocument(),
    );
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
