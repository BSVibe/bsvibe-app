/**
 * /oauth/consent — PWA-hosted OAuth Allow/Deny screen. Tests cover:
 *
 *  - Fetches client metadata + renders "Allow {client_name}…" + scopes.
 *  - Allow → POSTs `action=approve`, then top-level navigation to the
 *    backend's `redirect_to`.
 *  - Cancel → POSTs `action=deny`, top-level navigation to the
 *    `?error=access_denied` URL.
 *  - No session → redirect to /login with `?return_to=` preserving the
 *    full consent URL.
 *  - Unknown-client error UI on 404.
 *  - Upstream `?error=invalid_client` renders the clean error card.
 */

import { ConsentClient } from "@/app/oauth/consent/ConsentClient";
import { ApiError } from "@/lib/api/client";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => searchParams,
}));

const getOAuthClientByClientId = vi.fn();
const postOAuthAuthorize = vi.fn();
vi.mock("@/lib/api/oauth", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/oauth")>("@/lib/api/oauth");
  return {
    ...actual,
    getOAuthClientByClientId: (...args: unknown[]) => getOAuthClientByClientId(...args),
    postOAuthAuthorize: (...args: unknown[]) => postOAuthAuthorize(...args),
  };
});

let searchParams = new URLSearchParams();

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const CLIENT = {
  client_id: "dcr-abc123",
  client_name: "Claude Code",
  client_type: "public",
  redirect_uris: ["http://127.0.0.1/callback"],
  allowed_scopes: ["mcp:read", "mcp:write"],
};

const VALID_QUERY =
  "response_type=code" +
  "&client_id=dcr-abc123" +
  "&redirect_uri=http%3A%2F%2F127.0.0.1%3A49921%2Fcallback" +
  "&scope=mcp%3Aread+mcp%3Awrite" +
  "&state=s" +
  "&code_challenge=cc" +
  "&code_challenge_method=S256";

function setLocationFor(query: string) {
  // jsdom's window.location.href can't be reassigned by default; stub the
  // whole object so the consent page can set href without throwing.
  const hrefSetter = vi.fn();
  Object.defineProperty(window, "location", {
    writable: true,
    value: {
      pathname: "/oauth/consent",
      search: `?${query}`,
      set href(value: string) {
        hrefSetter(value);
      },
    },
  });
  return hrefSetter;
}

describe("oauth consent page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setSession(SESSION);
    searchParams = new URLSearchParams(VALID_QUERY);
    setLocationFor(VALID_QUERY);
  });

  afterEach(() => {
    clearSession();
    vi.restoreAllMocks();
  });

  it("renders the client name + scope descriptions", async () => {
    getOAuthClientByClientId.mockResolvedValue(CLIENT);
    render(<ConsentClient />);

    expect(
      await screen.findByRole("heading", { name: /Allow Claude Code to access BSVibe\?/i }),
    ).toBeInTheDocument();
    expect(screen.getByText("Read your products, runs, and knowledge.")).toBeInTheDocument();
    expect(screen.getByText("Send requests and approve Safe Mode actions.")).toBeInTheDocument();
  });

  it("Allow button POSTs action=approve and navigates to redirect_to", async () => {
    getOAuthClientByClientId.mockResolvedValue(CLIENT);
    postOAuthAuthorize.mockResolvedValue({
      redirect_to: "http://127.0.0.1:49921/callback?code=abc&state=s",
    });
    const hrefSetter = setLocationFor(VALID_QUERY);

    render(<ConsentClient />);

    const allow = await screen.findByRole("button", { name: "Allow" });
    allow.click();

    await waitFor(() => expect(postOAuthAuthorize).toHaveBeenCalledTimes(1));
    const [params, action] = postOAuthAuthorize.mock.calls[0];
    expect(action).toBe("approve");
    expect(params).toMatchObject({
      response_type: "code",
      client_id: "dcr-abc123",
      redirect_uri: "http://127.0.0.1:49921/callback",
      scope: "mcp:read mcp:write",
      state: "s",
      code_challenge: "cc",
      code_challenge_method: "S256",
    });
    await waitFor(() =>
      expect(hrefSetter).toHaveBeenCalledWith("http://127.0.0.1:49921/callback?code=abc&state=s"),
    );
  });

  it("Cancel button POSTs action=deny", async () => {
    getOAuthClientByClientId.mockResolvedValue(CLIENT);
    postOAuthAuthorize.mockResolvedValue({
      redirect_to: "http://127.0.0.1:49921/callback?error=access_denied&state=s",
    });

    render(<ConsentClient />);
    const cancel = await screen.findByRole("button", { name: "Cancel" });
    cancel.click();

    await waitFor(() => expect(postOAuthAuthorize).toHaveBeenCalledTimes(1));
    const [, action] = postOAuthAuthorize.mock.calls[0];
    expect(action).toBe("deny");
  });

  it("redirects unauthenticated users to /login with return_to", async () => {
    clearSession();
    sessionStorage.clear();
    render(<ConsentClient />);

    await waitFor(() => expect(replace).toHaveBeenCalledTimes(1));
    const arg = replace.mock.calls[0][0] as string;
    expect(arg.startsWith("/login?return_to=")).toBe(true);
    // The full consent URL (including the OAuth query) must round-trip.
    expect(decodeURIComponent(arg)).toContain("/oauth/consent");
    expect(decodeURIComponent(arg)).toContain("client_id=dcr-abc123");
    expect(getOAuthClientByClientId).not.toHaveBeenCalled();

    // Lift E11 — the load-bearing carrier is sessionStorage. The query
    // is just for the rendering pass on /login; the value the callback
    // page reads after the IdP round-trip lives here.
    const stashed = sessionStorage.getItem("bsvibe.return_to");
    expect(stashed).toContain("/oauth/consent");
    expect(stashed).toContain("client_id=dcr-abc123");
    expect(stashed).toContain("redirect_uri=http%3A%2F%2F127.0.0.1%3A49921%2Fcallback");
    sessionStorage.clear();
  });

  it("shows the unknown-client error card when the lookup 404s", async () => {
    getOAuthClientByClientId.mockRejectedValue(new ApiError(404, "unknown client"));
    render(<ConsentClient />);

    expect(
      await screen.findByText(
        /We don.?t recognise this application. Ask the developer to re-register it\./,
      ),
    ).toBeInTheDocument();
  });

  it("renders the upstream error card when the backend forwards ?error=", async () => {
    searchParams = new URLSearchParams(`error=invalid_client&${VALID_QUERY}`);
    render(<ConsentClient />);

    expect(
      await screen.findByText(
        /We don.?t recognise this application. Ask the developer to re-register it\./,
      ),
    ).toBeInTheDocument();
    expect(getOAuthClientByClientId).not.toHaveBeenCalled();
  });
});
