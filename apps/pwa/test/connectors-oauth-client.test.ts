/**
 * Connector OAuth client — wire contract against a mocked fetch
 * (lib/api/connectors.ts → backend /api/v1/connectors/oauth/{provider}/start).
 */

import { startConnectorOAuth } from "@/lib/api/connectors";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

function okFetch(body: unknown) {
  return vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
  );
}

describe("connector oauth client", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    clearSession();
  });

  it("POSTs to the provider start endpoint and returns the authorize_url", async () => {
    const fetchMock = okFetch({ authorize_url: "https://provider.example/authorize?x=1" });
    global.fetch = fetchMock as unknown as typeof fetch;

    const out = await startConnectorOAuth("github");

    expect(out.authorize_url).toBe("https://provider.example/authorize?x=1");
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(String(url)).toContain("/api/v1/connectors/oauth/github/start");
    expect(init?.method).toBe("POST");
  });

  it("url-encodes the provider segment", async () => {
    const fetchMock = okFetch({ authorize_url: "https://x" });
    global.fetch = fetchMock as unknown as typeof fetch;

    await startConnectorOAuth("email-sender");

    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(String(url)).toContain("/api/v1/connectors/oauth/email-sender/start");
  });
});
