/**
 * Executor workers surface — the Settings → Models "Executor workers" section
 * (Lift E4 — GitHub-Actions-runner UX; Lift E5 removed the legacy install-token
 * escape hatch).
 *
 *  - Calm empty state when no worker is registered (no cards)
 *  - LIST renders a card per worker (name, capability chips, online/offline pill,
 *    last-seen + added-on detail)
 *  - A failed list read degrades to a calm inline note (no crash)
 *  - "Add a worker" reveals the runner-style install snippet (no install-token
 *    paste): `bsvibe login && bsvibe-worker register --name $(hostname) &&
 *    bsvibe-worker run`
 *  - The legacy install-token affordance is GONE — no "show legacy" toggle,
 *    no mint button, no UI access to a deprecated path
 *  - Revoke: confirm → DELETE fires → re-read fires
 *
 * Determinism: the worker list loads asynchronously on mount, so every
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
  last_heartbeat: "2026-06-06T12:00:00+00:00",
  created_at: "2026-06-01T12:00:00+00:00",
};

const OFFLINE_WORKER = {
  id: "22222222-2222-2222-2222-222222222222",
  workspace_id: "ws-1",
  name: "old-laptop",
  labels: [],
  capabilities: ["opencode"],
  status: "offline",
  is_active: true,
  last_heartbeat: null,
  created_at: "2026-06-05T12:00:00+00:00",
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

  it("surfaces last-seen + added-on detail on each card", async () => {
    global.fetch = vi.fn(async () =>
      jsonResponse([ONLINE_WORKER, OFFLINE_WORKER]),
    ) as unknown as typeof fetch;

    render(<ExecutorWorkers />);

    const list = await screen.findByRole("list", { name: /workers/i });
    const onlineCard = within(list).getByText("studio-mini").closest("li") as HTMLElement;
    // ONLINE_WORKER has a last_heartbeat — surfaces "Last seen …".
    expect(within(onlineCard).getByText(/Last seen/i)).toBeInTheDocument();
    expect(within(onlineCard).getByText(/Added/i)).toBeInTheDocument();

    const offlineCard = within(list).getByText("old-laptop").closest("li") as HTMLElement;
    // OFFLINE_WORKER has no heartbeat — only "Added …" shows.
    expect(within(offlineCard).queryByText(/Last seen/i)).not.toBeInTheDocument();
    expect(within(offlineCard).getByText(/Added/i)).toBeInTheDocument();
  });

  it("surfaces a calm note when the list read fails", async () => {
    global.fetch = vi.fn(
      async () => new Response("boom", { status: 500 }),
    ) as unknown as typeof fetch;

    render(<ExecutorWorkers />);

    expect(await screen.findByText(/Couldn.t load your workers/i)).toBeInTheDocument();
  });

  it("Add a worker reveals the runner-style install snippet (no install token paste)", async () => {
    global.fetch = vi.fn(async () => jsonResponse([])) as unknown as typeof fetch;
    render(<ExecutorWorkers />);

    await screen.findByText(/No worker connected yet/i);
    await userEvent.click(screen.getByRole("button", { name: /add a worker/i }));

    // The new flow is `bsvibe login && bsvibe-worker register …` — NO install
    // token to paste, NO `python -m backend.executors.worker INSTALL_TOKEN=…`.
    const cmd = await screen.findByText(
      /bsvibe login && bsvibe-worker register --name \$\(hostname\) && bsvibe-worker run/,
    );
    expect(cmd).toBeInTheDocument();
    expect(screen.queryByText(/BSVIBE_WORKER_INSTALL_TOKEN=/i)).not.toBeInTheDocument();
    // The snippet points at THIS deployment's backend.
    expect(
      screen.getByText(/BSVIBE_WORKER_SERVER_URL=https:\/\/api\.bsvibe\.dev/i),
    ).toBeInTheDocument();
  });

  it("has no legacy install-token affordance (Lift E5 removed it)", async () => {
    global.fetch = vi.fn(async () => jsonResponse([])) as unknown as typeof fetch;
    render(<ExecutorWorkers />);

    await screen.findByText(/No worker connected yet/i);
    await userEvent.click(screen.getByRole("button", { name: /add a worker/i }));

    // After E5, NO toggle to reveal a legacy mint button, NO mint button at all.
    expect(screen.queryByRole("button", { name: /legacy install token/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /mint/i })).not.toBeInTheDocument();
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

    const confirm = await within(card).findByRole("button", { name: /^Confirm revoke$/i });
    await userEvent.click(confirm);

    await waitFor(() => {
      const deleteCall = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
      expect(deleteCall[0]).toBe(`/api/v1/workers/${ONLINE_WORKER.id}`);
      expect(deleteCall[1].method).toBe("DELETE");
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });
});
