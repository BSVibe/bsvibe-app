/**
 * Run-routing surface — Settings → Models → ROUTING (Lift N5, 2-column). The
 * founder's routing rules are now a table of `[NL condition text] [model]`:
 *
 *  - calm empty state; is_default rules hidden (the default is the picker above)
 *  - each rule is a row: condition (source_text, or matchLabel for legacy null
 *    rows) + the friendly model
 *  - Add: type a free-text condition + pick a model → POST {name, source_text, target}
 *  - Edit: inline text + select → PATCH {source_text, target}
 *  - Delete: confirm → DELETE
 *  - A 422 (uninterpretable condition) surfaces the rephrase hint inline, no crash
 *  - No "describe your routing" NL-compile panel; no caller/dimension dropdowns
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

// An N5 source_text rule: the condition is the founder's verbatim NL phrase.
const RULE = {
  id: "11111111-1111-1111-1111-111111111111",
  workspace_id: "22222222-2222-2222-2222-222222222222",
  name: "복잡한 작업",
  caller_id: null,
  source_text: "복잡한 작업",
  priority: 10,
  is_default: false,
  target: "opus",
  conditions: [{ field: "estimated_tokens", operator: "gt", value: 8000, negate: false }],
  is_active: true,
  created_at: "2026-07-11T00:00:00Z",
};

const DEFAULT_RULE = {
  ...RULE,
  id: "99999999-9999-9999-9999-999999999999",
  is_default: true,
  source_text: null,
  caller_id: null,
};

// A LEGACY structured rule (source_text null) — the condition column falls back
// to the human matchLabel (the stage caller's localized label).
const LEGACY_STAGE_RULE = {
  ...RULE,
  id: "33333333-3333-3333-3333-333333333333",
  name: "design → opus",
  source_text: null,
  caller_id: "workflow.agent_loop.plan",
  conditions: [],
};

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

type Hooks = {
  onPost?: () => void;
  onPatch?: () => void;
  onDelete?: () => void;
  postStatus?: number;
  postBody?: unknown;
  patchBody?: unknown;
};

function routedFetch(rules: unknown, hooks: Hooks = {}) {
  return vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    const method = (init?.method ?? "GET").toUpperCase();
    if (u.includes("/api/v1/accounts")) return jsonResponse(ACCOUNTS);
    if (u.endsWith("/api/v1/run-routing") && method === "POST") {
      hooks.onPost?.();
      if (hooks.postStatus && hooks.postStatus >= 400) {
        return jsonResponse(hooks.postBody ?? { detail: "boom" }, hooks.postStatus);
      }
      return jsonResponse(hooks.postBody ?? RULE, 201);
    }
    if (u.match(/\/api\/v1\/run-routing\/[^/]+$/) && method === "PATCH") {
      hooks.onPatch?.();
      return jsonResponse(hooks.patchBody ?? { ...RULE, target: "sonnet" });
    }
    if (u.match(/\/api\/v1\/run-routing\/[^/]+$/) && method === "DELETE") {
      hooks.onDelete?.();
      return jsonResponse(null, 204);
    }
    if (u.endsWith("/api/v1/run-routing")) return jsonResponse(rules);
    return jsonResponse([]);
  });
}

describe("Run-routing surface (Lift N5 — 2-column NL condition + model)", () => {
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

  it("has NO 'describe your routing' NL panel and NO caller/dimension dropdown", async () => {
    global.fetch = routedFetch([]) as unknown as typeof fetch;
    render(<RunRoutingRules />);
    await waitFor(() =>
      expect(screen.getByText(/All work goes to the active model account/i)).toBeInTheDocument(),
    );
    // The rejected NL-compile textarea is gone.
    expect(screen.queryByPlaceholderText(/Design work goes to opus/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Draft rules/i })).not.toBeInTheDocument();
    // The rejected caller dropdown is gone.
    expect(screen.queryByLabelText(/^Caller$/i)).not.toBeInTheDocument();
  });

  it("renders a source_text rule as a row: [condition] [friendly model]", async () => {
    global.fetch = routedFetch([RULE]) as unknown as typeof fetch;
    render(<RunRoutingRules />);
    const list = await screen.findByRole("list", { name: /Routing rules/i });
    // Column 1: the founder's verbatim NL condition phrase.
    await waitFor(() => expect(within(list).getByText("복잡한 작업")).toBeInTheDocument());
    // Column 2: the friendly account label, not the raw litellm id.
    expect(within(list).getByText("dogfood (opus)")).toBeInTheDocument();
    // The rule's freeform name is NOT separately shown when it equals source_text.
    expect(within(list).queryByText("estimated_tokens > 8000")).not.toBeInTheDocument();
  });

  it("renders a LEGACY null source_text rule via its human matchLabel", async () => {
    global.fetch = routedFetch([LEGACY_STAGE_RULE]) as unknown as typeof fetch;
    render(<RunRoutingRules />);
    const list = await screen.findByRole("list", { name: /Routing rules/i });
    // caller_id-based legacy rule → the localized stage label, never blank / raw id.
    await waitFor(() => expect(within(list).getByText("Design & planning")).toBeInTheDocument());
    expect(within(list).queryByText("workflow.agent_loop.plan")).not.toBeInTheDocument();
  });

  it("hides is_default rules (the default is the picker above)", async () => {
    global.fetch = routedFetch([DEFAULT_RULE]) as unknown as typeof fetch;
    render(<RunRoutingRules />);
    await waitFor(() =>
      expect(screen.getByText(/All work goes to the active model account/i)).toBeInTheDocument(),
    );
  });

  it("adds a row: type a condition + pick a model → POST {name, source_text, target}", async () => {
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
    await userEvent.type(screen.getByLabelText(/^Condition$/i), "마케팅 관련");
    await userEvent.selectOptions(screen.getByLabelText(/^Model$/i), "opus");
    await userEvent.click(screen.getByRole("button", { name: /^Add rule$/i }));

    await waitFor(() => expect(posted).toBe(true));
    const call = (fetchMock.mock.calls as unknown as Array<[string, RequestInit]>).find(
      ([u, i]) => String(u).endsWith("/api/v1/run-routing") && (i?.method ?? "GET") === "POST",
    );
    if (!call) throw new Error("expected POST");
    const body = JSON.parse(call[1].body as string);
    expect(body.source_text).toBe("마케팅 관련");
    expect(body.target).toBe("opus");
    expect(body.name).toBe("마케팅 관련");
    // The rejected structured fields are NOT sent.
    expect(body.caller_id).toBeUndefined();
    expect(body.is_default).toBeUndefined();
  });

  it("edits a row inline (text + select) → PATCH {source_text, target}", async () => {
    let patched = false;
    const fetchMock = routedFetch([RULE], {
      onPatch: () => {
        patched = true;
      },
    });
    global.fetch = fetchMock as unknown as typeof fetch;
    render(<RunRoutingRules />);
    const list = await screen.findByRole("list");
    await waitFor(() => expect(within(list).getByText("복잡한 작업")).toBeInTheDocument());

    await userEvent.click(within(list).getByRole("button", { name: /^Edit$/i }));
    const input = screen.getByLabelText(/^Condition$/i);
    await userEvent.clear(input);
    await userEvent.type(input, "한국어 요청");
    await userEvent.selectOptions(screen.getByLabelText(/^Model$/i), "sonnet");
    await userEvent.click(screen.getByRole("button", { name: /^Save$/i }));

    await waitFor(() => expect(patched).toBe(true));
    const call = (fetchMock.mock.calls as unknown as Array<[string, RequestInit]>).find(
      ([, i]) => (i?.method ?? "GET") === "PATCH",
    );
    if (!call) throw new Error("expected PATCH");
    const body = JSON.parse(call[1].body as string);
    expect(body.source_text).toBe("한국어 요청");
    expect(body.target).toBe("sonnet");
  });

  it("surfaces the 422 rephrase hint inline on add — and does not blow up", async () => {
    const fetchMock = routedFetch([], {
      postStatus: 422,
      postBody: { detail: "could not interpret the condition — try rephrasing" },
    });
    global.fetch = fetchMock as unknown as typeof fetch;
    render(<RunRoutingRules />);
    await waitFor(() =>
      expect(screen.getByText(/All work goes to the active model account/i)).toBeInTheDocument(),
    );

    await userEvent.click(screen.getByRole("button", { name: "+ Add rule" }));
    await userEvent.type(screen.getByLabelText(/^Condition$/i), "asdf qwer");
    await userEvent.selectOptions(screen.getByLabelText(/^Model$/i), "opus");
    await userEvent.click(screen.getByRole("button", { name: /^Add rule$/i }));

    // The inline rephrase hint appears; the row editor is still there (no crash).
    await waitFor(() =>
      expect(screen.getByText(/couldn.t interpret that condition/i)).toBeInTheDocument(),
    );
    expect(screen.getByLabelText(/^Condition$/i)).toBeInTheDocument();
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
    await waitFor(() => expect(within(list).getByText("복잡한 작업")).toBeInTheDocument());

    await userEvent.click(within(list).getByRole("button", { name: /^Remove$/i }));
    await userEvent.click(await screen.findByRole("button", { name: /^Confirm remove$/i }));
    await waitFor(() => expect(deleted).toBe(true));
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
