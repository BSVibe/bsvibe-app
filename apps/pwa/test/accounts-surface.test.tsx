/**
 * Model-accounts surface — the Settings → Model accounts section. Drives the
 * real list/create/activate/revoke clients against a mocked fetch and asserts:
 *
 *  - the load-bearing empty-state nudge when no accounts exist
 *  - list renders each account (label, provider·model, key-on-file, active)
 *  - Add: POST fires with the form body; the secret is never echoed; a success
 *    note (with the label, not the key) shows and a re-read fires
 *  - Activate / deactivate: PATCH { is_active } fires → re-read
 *  - Revoke: confirm → DELETE fires → re-read
 *  - calm inline error states never crash the surface
 */

import ModelAccounts from "@/components/settings/ModelAccounts";
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

const ACCOUNT = {
  id: "11111111-1111-1111-1111-111111111111",
  workspace_id: "ws-1",
  account_id: "acct-1",
  provider: "openai",
  label: "Primary",
  litellm_model: "gpt-5",
  api_base: null,
  data_jurisdiction: "us",
  is_active: true,
  has_api_key: true,
  extra_params: {},
  created_at: "2026-05-23T00:00:00Z",
  updated_at: "2026-05-23T00:00:00Z",
};

describe("Model accounts surface", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows the load-bearing empty-state nudge when there are no accounts", async () => {
    global.fetch = vi.fn(async () => jsonResponse([])) as unknown as typeof fetch;

    render(<ModelAccounts />);

    await waitFor(() => {
      expect(screen.getByText(/without it I can.t run work/i)).toBeInTheDocument();
    });
  });

  it("lists registered accounts with label, provider·model, key flag and state", async () => {
    global.fetch = vi.fn(async () => jsonResponse([ACCOUNT])) as unknown as typeof fetch;

    render(<ModelAccounts />);

    await waitFor(() => expect(screen.getByText("Primary")).toBeInTheDocument());
    expect(screen.getByText(/openai · gpt-5/)).toBeInTheDocument();
    expect(screen.getByText(/key on file/i)).toBeInTheDocument();
    expect(screen.getByText(/^Active$/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Deactivate$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Revoke$/i })).toBeInTheDocument();
  });

  it("creates an account, POSTs the body, confirms without echoing the secret, re-reads", async () => {
    const fetchMock = vi
      .fn()
      // initial list (empty)
      .mockResolvedValueOnce(jsonResponse([]))
      // create
      .mockResolvedValueOnce(jsonResponse(ACCOUNT, 201))
      // re-read after create
      .mockResolvedValueOnce(jsonResponse([ACCOUNT]));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<ModelAccounts />);
    await waitFor(() =>
      expect(screen.getByText(/without it I can.t run work/i)).toBeInTheDocument(),
    );

    // The founder-facing jurisdiction picker is gone — invisible infra.
    expect(screen.queryByLabelText(/Data jurisdiction/i)).not.toBeInTheDocument();

    // The add form is collapsed by default — open it before filling it in.
    expect(screen.queryByLabelText(/^Provider$/i)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "+ Add account" }));

    await userEvent.type(screen.getByLabelText(/^Provider$/i), "openai");
    await userEvent.type(screen.getByLabelText(/Model identifier/i), "gpt-5");
    await userEvent.type(screen.getByLabelText(/^Label$/i), "Primary");
    await userEvent.type(screen.getByLabelText(/^API key/i), "sk-super-secret");
    await userEvent.click(screen.getByRole("button", { name: /^Add model account$/i }));

    // On success the form collapses and the list re-reads — the new account
    // row (label, no key) is the confirmation; the secret is never echoed.
    await waitFor(() => {
      expect(screen.getByText("Primary")).toBeInTheDocument();
    });
    expect(screen.queryByText("sk-super-secret")).not.toBeInTheDocument();
    // The add form collapsed back (its inputs are gone again).
    expect(screen.queryByLabelText(/^Provider$/i)).not.toBeInTheDocument();

    // The create POST carried the form body, including the plaintext key once.
    const createCall = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
    expect(createCall[0]).toBe("/api/v1/accounts");
    expect(createCall[1].method).toBe("POST");
    // The body no longer carries data_jurisdiction — the backend defaults it.
    const sentBody = JSON.parse(createCall[1].body as string);
    expect(sentBody).toEqual({
      provider: "openai",
      label: "Primary",
      litellm_model: "gpt-5",
      api_key: "sk-super-secret",
      extra_params: {},
    });
    expect("data_jurisdiction" in sentBody).toBe(false);

    // A re-read fired (3rd call is the list GET).
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("activates an inactive account via PATCH { is_active: true } then re-reads", async () => {
    const inactive = { ...ACCOUNT, is_active: false };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([inactive]))
      .mockResolvedValueOnce(jsonResponse(ACCOUNT))
      .mockResolvedValueOnce(jsonResponse([ACCOUNT]));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<ModelAccounts />);
    const list = await screen.findByRole("list");
    await waitFor(() => expect(within(list).getByText("Primary")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: /^Activate$/i }));

    await waitFor(() => {
      const patchCall = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
      expect(patchCall[0]).toBe(`/api/v1/accounts/${ACCOUNT.id}`);
      expect(patchCall[1].method).toBe("PATCH");
      expect(JSON.parse(patchCall[1].body as string)).toEqual({ is_active: true });
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("re-enables the row's actions after a successful toggle (not stuck disabled)", async () => {
    // Regression: a toggle keeps the same account in the list (same React key),
    // so the row instance persists. It must reset to idle after success, else
    // the buttons stay permanently disabled. Surfaced in the Playwright run.
    const inactive = { ...ACCOUNT, is_active: false };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([inactive]))
      .mockResolvedValueOnce(jsonResponse(ACCOUNT))
      .mockResolvedValueOnce(jsonResponse([ACCOUNT]));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<ModelAccounts />);
    const list = await screen.findByRole("list");
    await waitFor(() => expect(within(list).getByText("Primary")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: /^Activate$/i }));

    // After the re-read the row reflects Active and its actions are usable again.
    await waitFor(() =>
      expect(within(list).getByRole("button", { name: /^Deactivate$/i })).toBeEnabled(),
    );
    expect(within(list).getByRole("button", { name: /^Revoke$/i })).toBeEnabled();
  });

  it("warns when accounts exist but none are active", async () => {
    const inactive = { ...ACCOUNT, is_active: false };
    global.fetch = vi.fn(async () => jsonResponse([inactive])) as unknown as typeof fetch;

    render(<ModelAccounts />);

    await waitFor(() => {
      expect(screen.getByText(/activate one so I can run work/i)).toBeInTheDocument();
    });
  });

  it("revokes an account after confirm → DELETE → re-read", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([ACCOUNT]))
      .mockResolvedValueOnce(jsonResponse(null, 204))
      .mockResolvedValueOnce(jsonResponse([]));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<ModelAccounts />);
    const list = await screen.findByRole("list");
    await waitFor(() => expect(within(list).getByText("Primary")).toBeInTheDocument());

    const row = within(list).getByText("Primary").closest("li") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: /^Revoke$/i }));
    const confirm = await within(row).findByRole("button", { name: /^Confirm revoke$/i });
    await userEvent.click(confirm);

    await waitFor(() => {
      const deleteCall = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
      expect(deleteCall[0]).toBe(`/api/v1/accounts/${ACCOUNT.id}`);
      expect(deleteCall[1].method).toBe("DELETE");
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("shows a calm inline error when create fails and keeps the form usable", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([]))
      .mockResolvedValueOnce(jsonResponse("bad", 422));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<ModelAccounts />);
    await waitFor(() =>
      expect(screen.getByText(/without it I can.t run work/i)).toBeInTheDocument(),
    );

    await userEvent.click(screen.getByRole("button", { name: "+ Add account" }));
    await userEvent.type(screen.getByLabelText(/^Provider$/i), "openai");
    await userEvent.type(screen.getByLabelText(/Model identifier/i), "gpt-5");
    await userEvent.type(screen.getByLabelText(/^Label$/i), "Primary");
    await userEvent.type(screen.getByLabelText(/^API key/i), "k");
    await userEvent.click(screen.getByRole("button", { name: /^Add model account$/i }));

    expect(await screen.findByText(/Couldn.t add that model account/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Add model account$/i })).toBeEnabled();
  });

  it("surfaces a calm note when the list read fails", async () => {
    global.fetch = vi.fn(
      async () => new Response("boom", { status: 500 }),
    ) as unknown as typeof fetch;

    render(<ModelAccounts />);

    await waitFor(() => {
      expect(screen.getByText(/Couldn.t load your model accounts/i)).toBeInTheDocument();
    });
  });
});
