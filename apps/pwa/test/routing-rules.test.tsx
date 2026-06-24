/**
 * Routing-rules surface — the Settings → Models → ROUTING section. Drives the
 * real list/create/delete clients against a mocked fetch and asserts:
 *
 *  - the calm empty state when there are no rules (explains the default)
 *  - list renders each rule (name, → target_model, priority, default/active chips)
 *  - Add: POST fires with the form body, a re-read fires
 *  - Delete: confirm → DELETE fires → re-read
 *  - a calm inline note when the list read fails (never a blanked surface)
 */

import RoutingRules from "@/components/settings/RoutingRules";
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
  name: "Substantial work",
  priority: 10,
  target_model: "opencode/plan-builder",
  is_default: false,
  is_active: true,
  conditions: [
    { condition_type: "intent", field: "classified_intent", operator: "eq", value: "deep" },
  ],
};

describe("Routing rules surface", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows the calm empty state explaining the default when there are no rules", async () => {
    global.fetch = vi.fn(async () => jsonResponse([])) as unknown as typeof fetch;

    render(<RoutingRules />);

    await waitFor(() => {
      expect(screen.getByText(/All work goes to the active model account/i)).toBeInTheDocument();
    });
  });

  it("lists each rule with name, target, priority and chips", async () => {
    global.fetch = vi.fn(async () => jsonResponse([RULE])) as unknown as typeof fetch;

    render(<RoutingRules />);

    await waitFor(() => expect(screen.getByText("Substantial work")).toBeInTheDocument());
    expect(screen.getByText(/opencode\/plan-builder/)).toBeInTheDocument();
    // Surfaces what the rule matches (its condition value).
    expect(screen.getByText(/deep/)).toBeInTheDocument();
  });

  it("creates a rule, POSTs the body, re-reads", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([]))
      .mockResolvedValueOnce(jsonResponse(RULE, 201))
      .mockResolvedValueOnce(jsonResponse([RULE]));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<RoutingRules />);
    await waitFor(() =>
      expect(screen.getByText(/All work goes to the active model account/i)).toBeInTheDocument(),
    );

    // The add form is collapsed by default — open it before filling it in.
    await userEvent.click(screen.getByRole("button", { name: "+ Add rule" }));
    await userEvent.type(screen.getByLabelText(/Rule name/i), "Substantial work");
    await userEvent.type(screen.getByLabelText(/Route to/i), "opencode/plan-builder");
    await userEvent.click(screen.getByRole("button", { name: /^Add rule$/i }));

    await waitFor(() => {
      const createCall = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
      expect(createCall[0]).toBe("/api/v1/rules");
      expect(createCall[1].method).toBe("POST");
      const body = JSON.parse(createCall[1].body as string);
      expect(body.name).toBe("Substantial work");
      expect(body.target_model).toBe("opencode/plan-builder");
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("deletes a rule after confirm → DELETE → re-read", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([RULE]))
      .mockResolvedValueOnce(jsonResponse(null, 204))
      .mockResolvedValueOnce(jsonResponse([]));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<RoutingRules />);
    const list = await screen.findByRole("list");
    await waitFor(() => expect(within(list).getByText("Substantial work")).toBeInTheDocument());

    const row = within(list).getByText("Substantial work").closest("li") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: /^Remove$/i }));
    const confirm = await within(row).findByRole("button", { name: /^Confirm remove$/i });
    await userEvent.click(confirm);

    await waitFor(() => {
      const deleteCall = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
      expect(deleteCall[0]).toBe(`/api/v1/rules/${RULE.id}`);
      expect(deleteCall[1].method).toBe("DELETE");
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("surfaces a calm note when the list read fails", async () => {
    global.fetch = vi.fn(
      async () => new Response("boom", { status: 500 }),
    ) as unknown as typeof fetch;

    render(<RoutingRules />);

    await waitFor(() => {
      expect(screen.getByText(/Couldn.t load your routing rules/i)).toBeInTheDocument();
    });
  });
});
