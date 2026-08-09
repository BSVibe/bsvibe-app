/**
 * /device — the browser half of RFC 8628.
 *
 * The device that started this has no browser and cannot receive a redirect.
 * It is polling `/token` right now, so this screen's only job is to let the
 * human say yes or no to a request they can actually read. Nothing is handed
 * back to the device from here — that is the property the whole flow rests on.
 *
 * What is pinned:
 *  - the code can arrive in the URL (`verification_uri_complete`) or be typed;
 *  - the human sees WHAT is being granted before approving, never a bare
 *    "approve?" prompt;
 *  - approve and deny are both real, terminal outcomes with visible feedback;
 *  - an already-decided or expired code says so instead of offering a button
 *    that will fail;
 *  - a signed-out visitor is bounced to login and comes back to this URL.
 */

import { DeviceConsentClient } from "@/app/device/DeviceConsentClient";
import { ApiError } from "@/lib/api/client";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => searchParams,
}));

const getDeviceRequest = vi.fn();
const decideDeviceRequest = vi.fn();
vi.mock("@/lib/api/oauth", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/oauth")>("@/lib/api/oauth");
  return {
    ...actual,
    getDeviceRequest: (...args: unknown[]) => getDeviceRequest(...args),
    decideDeviceRequest: (...args: unknown[]) => decideDeviceRequest(...args),
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

const PENDING = {
  client_id: "dcr-device-cli",
  scope: ["mcp:read", "mcp:write", "mcp:admin"],
  status: "pending" as const,
  expires_at: new Date(Date.now() + 600_000).toISOString(),
};

describe("/device — device authorization consent", () => {
  beforeEach(() => {
    searchParams = new URLSearchParams();
    setSession(SESSION);
    getDeviceRequest.mockResolvedValue(PENDING);
    decideDeviceRequest.mockResolvedValue({ ...PENDING, status: "approved" });
  });
  afterEach(() => {
    clearSession();
    vi.restoreAllMocks();
    vi.clearAllMocks();
  });

  it("prefills the code from verification_uri_complete and shows the scopes", async () => {
    searchParams = new URLSearchParams("user_code=WXYZ-2345");

    render(<DeviceConsentClient />);

    await waitFor(() => expect(getDeviceRequest).toHaveBeenCalledWith("WXYZ-2345"));
    // The human must see what they are granting, not a bare "approve?".
    expect(await screen.findByText("mcp:admin")).toBeInTheDocument();
    expect(screen.getByText("mcp:read")).toBeInTheDocument();
  });

  it("lets the code be typed when the URL carries none", async () => {
    render(<DeviceConsentClient />);

    const input = await screen.findByLabelText(/code/i);
    await userEvent.type(input, "WXYZ-2345");
    await userEvent.click(screen.getByRole("button", { name: /continue/i }));

    await waitFor(() => expect(getDeviceRequest).toHaveBeenCalledWith("WXYZ-2345"));
  });

  it("approves and reports success without handing anything back", async () => {
    searchParams = new URLSearchParams("user_code=WXYZ-2345");
    render(<DeviceConsentClient />);
    await screen.findByText("mcp:admin");

    await userEvent.click(screen.getByRole("button", { name: /^allow$/i }));

    await waitFor(() => expect(decideDeviceRequest).toHaveBeenCalledWith("WXYZ-2345", true));
    // The device is polling; the human is told they can walk away.
    expect(await screen.findByText(/you can close this/i)).toBeInTheDocument();
  });

  it("denies as a real terminal outcome", async () => {
    searchParams = new URLSearchParams("user_code=WXYZ-2345");
    decideDeviceRequest.mockResolvedValue({ ...PENDING, status: "denied" });
    render(<DeviceConsentClient />);
    await screen.findByText("mcp:admin");

    await userEvent.click(screen.getByRole("button", { name: /^deny$/i }));

    await waitFor(() => expect(decideDeviceRequest).toHaveBeenCalledWith("WXYZ-2345", false));
    expect(await screen.findByText(/denied/i)).toBeInTheDocument();
  });

  it("does not offer a button for an already-decided code", async () => {
    searchParams = new URLSearchParams("user_code=WXYZ-2345");
    getDeviceRequest.mockResolvedValue({ ...PENDING, status: "approved" });

    render(<DeviceConsentClient />);

    expect(await screen.findByText(/already approved/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^allow$/i })).not.toBeInTheDocument();
  });

  it("says a code is expired rather than failing on approve", async () => {
    searchParams = new URLSearchParams("user_code=WXYZ-2345");
    getDeviceRequest.mockResolvedValue({ ...PENDING, status: "expired" });

    render(<DeviceConsentClient />);

    expect(await screen.findByText(/expired/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^allow$/i })).not.toBeInTheDocument();
  });

  it("surfaces an unknown code instead of a blank screen", async () => {
    searchParams = new URLSearchParams("user_code=ZZZZ-ZZZZ");
    getDeviceRequest.mockRejectedValue(new ApiError(404, "not found"));

    render(<DeviceConsentClient />);

    expect(await screen.findByText(/didn.t match|not found|no request/i)).toBeInTheDocument();
  });

  it("bounces a signed-out visitor to login and back", async () => {
    clearSession();
    searchParams = new URLSearchParams("user_code=WXYZ-2345");

    render(<DeviceConsentClient />);

    await waitFor(() => expect(replace).toHaveBeenCalled());
    expect(String(replace.mock.calls[0][0])).toContain("/login?return_to=");
  });
});
