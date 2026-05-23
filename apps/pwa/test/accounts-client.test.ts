/**
 * Model-accounts client — wire contracts against a mocked fetch
 * (lib/api/accounts.ts → backend /api/v1/accounts).
 *
 *  - listAccounts:      GET /api/v1/accounts
 *  - createAccount:     POST /api/v1/accounts with the extra=forbid body
 *                       (drops blank api_base, always sends extra_params; the
 *                       plaintext api_key is sent once, never read back)
 *  - setAccountActive:  PATCH /api/v1/accounts/{id} { is_active }
 *  - updateAccount:     PATCH /api/v1/accounts/{id} with only the given keys
 *  - revokeAccount:     DELETE /api/v1/accounts/{id} → 204 void
 */

import {
  createAccount,
  listAccounts,
  revokeAccount,
  setAccountActive,
  updateAccount,
} from "@/lib/api/accounts";
import { ApiError } from "@/lib/api/client";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

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

function okFetch(body: unknown, status = 200) {
  return vi.fn(
    async () =>
      new Response(status === 204 ? null : JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
  );
}

describe("accounts client", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("listAccounts GETs /api/v1/accounts", async () => {
    const fetchMock = okFetch([ACCOUNT]);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await listAccounts();

    expect(res).toEqual([ACCOUNT]);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/accounts");
    expect((init.method ?? "GET").toUpperCase()).toBe("GET");
  });

  it("createAccount POSTs the full body and never reads the secret back", async () => {
    const fetchMock = okFetch(ACCOUNT, 201);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await createAccount({
      provider: "openai",
      label: "Primary",
      litellm_model: "gpt-5",
      api_key: "sk-super-secret",
      data_jurisdiction: "us",
      api_base: "https://proxy.example.com",
    });

    expect(res).toEqual(ACCOUNT);
    // The response carries the masked flag, never the key.
    expect("api_key" in res).toBe(false);
    expect(res.has_api_key).toBe(true);

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/accounts");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      provider: "openai",
      label: "Primary",
      litellm_model: "gpt-5",
      api_key: "sk-super-secret",
      data_jurisdiction: "us",
      extra_params: {},
      api_base: "https://proxy.example.com",
    });
  });

  it("createAccount drops a blank api_base and defaults extra_params to {}", async () => {
    const fetchMock = okFetch(ACCOUNT, 201);
    global.fetch = fetchMock as unknown as typeof fetch;

    await createAccount({
      provider: "anthropic",
      label: "Claude",
      litellm_model: "claude-sonnet-4",
      api_key: "sk-x",
      data_jurisdiction: "eu",
      api_base: "   ",
    });

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect("api_base" in body).toBe(false);
    expect(body.extra_params).toEqual({});
  });

  it("setAccountActive PATCHes /api/v1/accounts/{id} with just { is_active }", async () => {
    const fetchMock = okFetch({ ...ACCOUNT, is_active: false });
    global.fetch = fetchMock as unknown as typeof fetch;

    await setAccountActive(ACCOUNT.id, false);

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`/api/v1/accounts/${ACCOUNT.id}`);
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({ is_active: false });
  });

  it("updateAccount sends only the provided keys", async () => {
    const fetchMock = okFetch(ACCOUNT);
    global.fetch = fetchMock as unknown as typeof fetch;

    await updateAccount(ACCOUNT.id, { label: "Renamed" });

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ label: "Renamed" });
  });

  it("revokeAccount DELETEs /api/v1/accounts/{id} and resolves void", async () => {
    const fetchMock = okFetch(null, 204);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await revokeAccount(ACCOUNT.id);

    expect(res).toBeUndefined();
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`/api/v1/accounts/${ACCOUNT.id}`);
    expect(init.method).toBe("DELETE");
  });

  it("surfaces an ApiError on a non-ok list read", async () => {
    global.fetch = vi.fn(
      async () => new Response("forbidden", { status: 403 }),
    ) as unknown as typeof fetch;

    await expect(listAccounts()).rejects.toBeInstanceOf(ApiError);
  });

  it("surfaces an ApiError on a non-ok create (e.g. 422)", async () => {
    global.fetch = vi.fn(
      async () => new Response("unprocessable", { status: 422 }),
    ) as unknown as typeof fetch;

    await expect(
      createAccount({
        provider: "openai",
        label: "x",
        litellm_model: "gpt-5",
        api_key: "k",
        data_jurisdiction: "us",
      }),
    ).rejects.toBeInstanceOf(ApiError);
  });
});
