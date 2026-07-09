/**
 * LocaleSync — the chrome-locale reconciler.
 *
 * `workspaces.language` is the SOURCE OF TRUTH for BOTH server-rendered content
 * and UI chrome (founder decision 2026-07). Because auth lives in client-side
 * localStorage, the server can't read the workspace at request time — it only
 * sees the `bsvibe.locale` cookie. So on app load (and workspace switch) the
 * client mirrors the active workspace's language into that cookie and refreshes,
 * keeping the chrome consistent with the content even on a fresh device / stale
 * cookie.
 */

import LocaleSync from "@/components/shell/LocaleSync";
import { LOCALE_COOKIE } from "@/lib/i18n/config";
import { render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const refresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh, replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
}));

const getWorkspace = vi.fn();
vi.mock("@/lib/api/workspace", () => ({
  getWorkspace: () => getWorkspace(),
}));

const getSessionMock = vi.fn();
vi.mock("@/lib/auth/session", () => ({
  getSession: () => getSessionMock(),
}));

function setCookie(value: string | null) {
  document.cookie = value
    ? `${LOCALE_COOKIE}=${value}; path=/`
    : `${LOCALE_COOKIE}=; path=/; max-age=0`;
}

describe("LocaleSync", () => {
  beforeEach(() => {
    refresh.mockClear();
    getWorkspace.mockReset();
    getSessionMock.mockReset();
    getSessionMock.mockReturnValue({ accessToken: "tok", userId: "u1" });
    setCookie(null);
  });
  afterEach(() => setCookie(null));

  it("syncs the cookie to the workspace language and refreshes when they differ", async () => {
    setCookie("en");
    getWorkspace.mockResolvedValue({ id: "w1", name: "W", language: "ko" });

    render(<LocaleSync />);

    await waitFor(() => expect(document.cookie).toContain(`${LOCALE_COOKIE}=ko`));
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("does NOT refresh when the cookie already matches the workspace language", async () => {
    setCookie("ko");
    getWorkspace.mockResolvedValue({ id: "w1", name: "W", language: "ko" });

    render(<LocaleSync />);

    await waitFor(() => expect(getWorkspace).toHaveBeenCalled());
    expect(refresh).not.toHaveBeenCalled();
    expect(document.cookie).toContain(`${LOCALE_COOKIE}=ko`);
  });

  it("skips entirely when logged out (no session, no fetch)", async () => {
    getSessionMock.mockReturnValue(null);

    render(<LocaleSync />);

    await Promise.resolve();
    expect(getWorkspace).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("is best-effort — a workspace fetch failure never throws or refreshes", async () => {
    setCookie("en");
    getWorkspace.mockRejectedValue(new Error("boom"));

    render(<LocaleSync />);

    await waitFor(() => expect(getWorkspace).toHaveBeenCalled());
    expect(refresh).not.toHaveBeenCalled();
  });
});
