/**
 * Executor workers surface — the Settings → Models "Executor workers" section.
 * Drives the real list/mint/revoke clients against a mocked fetch and asserts:
 *
 *  - calm empty state when no worker is registered (no cards)
 *  - LIST renders a card per worker (name, capability chips, online/offline pill)
 *  - a failed list read degrades to a calm inline note (no crash)
 *  - Connect → mintInstallToken → the one-time token is revealed once with a
 *    "won't see this again" note AND the run command (env vars + the
 *    `python -m backend.executors.worker` invocation)
 *  - Revoke: confirm → DELETE fires → re-read fires
 *
 * Determinism note: the worker list loads asynchronously on mount, so every
 * assertion that depends on it is gated behind `findBy*`/`waitFor`.
 */

import ExecutorWorkers from "@/components/settings/ExecutorWorkers";
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

const ONLINE_WORKER = {
  id: "11111111-1111-1111-1111-111111111111",
  workspace_id: "ws-1",
  name: "studio-mini",
  labels: ["mac"],
  capabilities: ["claude_code", "codex"],
  status: "online",
  is_active: true,
};

const OFFLINE_WORKER = {
  id: "22222222-2222-2222-2222-222222222222",
  workspace_id: "ws-1",
  name: "old-laptop",
  labels: [],
  capabilities: ["opencode"],
  status: "offline",
  is_active: true,
};

describe("Executor workers surface", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("keeps an Executor workers heading so the Models tab can find it", async () => {
    global.fetch = vi.fn(async () => jsonResponse([])) as unknown as typeof fetch;
    render(<ExecutorWorkers />);
    expect(screen.getByRole("heading", { name: /executor workers/i })).toBeInTheDocument();
    // Settle the async load so the test doesn't leak an unawaited state update.
    await screen.findByText(/No worker connected yet/i);
  });

  it("shows a calm empty state when no worker is registered", async () => {
    global.fetch = vi.fn(async () => jsonResponse([])) as unknown as typeof fetch;

    render(<ExecutorWorkers />);

    expect(await screen.findByText(/No worker connected yet/i)).toBeInTheDocument();
  });

  it("renders a worker card per worker with name, capability chips, and an online pill", async () => {
    global.fetch = vi.fn(async () =>
      jsonResponse([ONLINE_WORKER, OFFLINE_WORKER]),
    ) as unknown as typeof fetch;

    render(<ExecutorWorkers />);

    const list = await screen.findByRole("list", { name: /workers/i });
    const onlineCard = within(list).getByText("studio-mini").closest("li") as HTMLElement;
    expect(within(onlineCard).getByText("claude_code")).toBeInTheDocument();
    expect(within(onlineCard).getByText("codex")).toBeInTheDocument();
    expect(within(onlineCard).getByText(/^Online$/i)).toBeInTheDocument();

    const offlineCard = within(list).getByText("old-laptop").closest("li") as HTMLElement;
    expect(within(offlineCard).getByText("opencode")).toBeInTheDocument();
    expect(within(offlineCard).getByText(/^Offline$/i)).toBeInTheDocument();
  });

  it("surfaces a calm note when the list read fails", async () => {
    global.fetch = vi.fn(
      async () => new Response("boom", { status: 500 }),
    ) as unknown as typeof fetch;

    render(<ExecutorWorkers />);

    expect(await screen.findByText(/Couldn.t load your workers/i)).toBeInTheDocument();
  });

  it("Connect → mints the install token, reveals it once with the run command", async () => {
    const fetchMock = vi
      .fn()
      // initial list (empty)
      .mockResolvedValueOnce(jsonResponse([]))
      // mint install token
      .mockResolvedValueOnce(jsonResponse({ token: "INSTALL-TOKEN-once-abcd" }));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<ExecutorWorkers />);

    await screen.findByText(/No worker connected yet/i);
    await userEvent.click(screen.getByRole("button", { name: /connect a worker/i }));

    // The one-time token is revealed with a "won't see again" note.
    await waitFor(() => {
      expect(screen.getByText("INSTALL-TOKEN-once-abcd")).toBeInTheDocument();
    });
    expect(screen.getByText(/won.t see (this|it) again/i)).toBeInTheDocument();
    // The run command (the worker-process invocation) is shown, copyable.
    expect(screen.getByText(/python -m backend\.executors\.worker/i)).toBeInTheDocument();
    // It points the worker at THIS deployment's backend so a copy-paste run
    // actually reaches the server instead of the localhost default. (No
    // NEXT_PUBLIC_BACKEND_URL in the test env → the prod default URL.)
    expect(
      screen.getByText(/BSVIBE_WORKER_SERVER_URL=https:\/\/api\.bsvibe\.dev/i),
    ).toBeInTheDocument();

    // The mint POST fired against the install-token endpoint.
    const mintCall = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
    expect(mintCall[0]).toBe("/api/v1/workers/install-token");
    expect(mintCall[1].method).toBe("POST");
  });

  it("revokes a worker after confirm → DELETE → re-read", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([ONLINE_WORKER]))
      .mockResolvedValueOnce(jsonResponse(null, 204))
      .mockResolvedValueOnce(jsonResponse([]));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<ExecutorWorkers />);

    const list = await screen.findByRole("list", { name: /workers/i });
    const card = within(list).getByText("studio-mini").closest("li") as HTMLElement;
    await userEvent.click(within(card).getByRole("button", { name: /^Revoke$/i }));

    // Confirm affordance appears; clicking it fires the DELETE.
    const confirm = await within(card).findByRole("button", { name: /^Confirm revoke$/i });
    await userEvent.click(confirm);

    await waitFor(() => {
      const deleteCall = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
      expect(deleteCall[0]).toBe(`/api/v1/workers/${ONLINE_WORKER.id}`);
      expect(deleteCall[1].method).toBe("DELETE");
    });
    // A re-read fired after the revoke.
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("shows a calm inline error when mint fails and stays usable", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([]))
      .mockResolvedValueOnce(jsonResponse("forbidden", 403));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<ExecutorWorkers />);

    await screen.findByText(/No worker connected yet/i);
    await userEvent.click(screen.getByRole("button", { name: /connect a worker/i }));

    expect(await screen.findByText(/Couldn.t mint an install token/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /connect a worker/i })).toBeEnabled();
  });
});
