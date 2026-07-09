/**
 * General tab — the must-work surface of this lift.
 *
 *  - Theme: a Light / System / Dark segmented control wired to the theme
 *    controller. Choosing one persists the preference (bsvibe.theme) AND applies
 *    `data-theme` to <html>.
 *  - Language / Time zone / Date format: local-only preferences that persist
 *    their selected value to localStorage.
 *  - Workspace ID: display-only (the session's personal account id, or a calm
 *    fallback). No backend writes.
 */

import GeneralTab from "@/components/settings/GeneralTab";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { LOCALE_COOKIE } from "@/lib/i18n/config";
import { PREF_STORAGE_KEY } from "@/lib/preferences/preferences";
import { THEME_STORAGE_KEY } from "@/lib/theme/theme";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// GeneralTab refreshes the route after switching the locale; stub the router.
const refresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh, replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
}));

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
  personalAccountId: "acct-abc-123",
};

beforeEach(() => {
  window.localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
  document.cookie = `${LOCALE_COOKIE}=; path=/; max-age=0`;
  refresh.mockClear();
  clearSession();
  setSession(SESSION);
  // jsdom lacks matchMedia — provide a light-preferring stub.
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      matches: false,
      media: "(prefers-color-scheme: dark)",
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => true,
      onchange: null,
    })),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("General tab — theme control", () => {
  it("renders Light / System / Dark options", () => {
    render(<GeneralTab />);
    expect(screen.getByRole("radio", { name: /light/i })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /system/i })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /dark/i })).toBeInTheDocument();
  });

  it("choosing Dark persists the preference and applies data-theme=dark", async () => {
    const user = userEvent.setup();
    render(<GeneralTab />);

    await user.click(screen.getByRole("radio", { name: /dark/i }));

    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("choosing Light persists the preference and applies data-theme=light", async () => {
    const user = userEvent.setup();
    render(<GeneralTab />);

    await user.click(screen.getByRole("radio", { name: /dark/i }));
    await user.click(screen.getByRole("radio", { name: /light/i }));

    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
    expect(document.documentElement.dataset.theme).toBe("light");
  });
});

// workspaces.language is the source of truth for BOTH content and chrome, so
// choosing a language PATCHes the workspace FIRST and only then mirrors the
// cookie + refreshes. Stub the PATCH so the write succeeds.
function stubWorkspaceOk() {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify({ id: "ws-1", name: "W", language: "ko", safe_mode: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    ),
  );
}

describe("General tab — display preferences", () => {
  it("persists the selected language", async () => {
    stubWorkspaceOk();
    const user = userEvent.setup();
    render(<GeneralTab />);

    await user.selectOptions(screen.getByLabelText(/language/i), "ko");

    await waitFor(() =>
      expect(JSON.parse(window.localStorage.getItem(PREF_STORAGE_KEY) ?? "{}").language).toBe("ko"),
    );
  });

  it("persists the selected time zone", async () => {
    const user = userEvent.setup();
    render(<GeneralTab />);

    await user.selectOptions(screen.getByLabelText(/time zone/i), "UTC");

    expect(JSON.parse(window.localStorage.getItem(PREF_STORAGE_KEY) ?? "{}").timezone).toBe("UTC");
  });

  it("persists the selected date format", async () => {
    const user = userEvent.setup();
    render(<GeneralTab />);

    await user.selectOptions(screen.getByLabelText(/date format/i), "us");

    expect(JSON.parse(window.localStorage.getItem(PREF_STORAGE_KEY) ?? "{}").dateFormat).toBe("us");
  });

  it("switching language persists the workspace, then mirrors the cookie and refreshes", async () => {
    stubWorkspaceOk();
    const user = userEvent.setup();
    render(<GeneralTab />);

    await user.selectOptions(screen.getByLabelText(/language/i), "ko");

    await waitFor(() => expect(document.cookie).toContain(`${LOCALE_COOKIE}=ko`));
    expect(refresh).toHaveBeenCalled();
  });

  it("surfaces an error and does NOT desync the cookie when the workspace write fails", async () => {
    // The silent-fail bug: previously the cookie (chrome) was written even when
    // the workspace PATCH (content language) failed, desyncing the two. Now a
    // failed write surfaces an error and leaves the cookie untouched.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("nope", { status: 500 })),
    );
    const user = userEvent.setup();
    render(<GeneralTab />);

    await user.selectOptions(screen.getByLabelText(/language/i), "ko");

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(document.cookie).not.toContain(`${LOCALE_COOKIE}=ko`);
    expect(refresh).not.toHaveBeenCalled();
  });
});

describe("General tab — workspace identity", () => {
  it("shows the session account id as a placeholder while the workspace loads", () => {
    render(<GeneralTab />);
    // Before GET /api/v1/workspace resolves, the field falls back to the
    // session's personal account id so it is never blank.
    expect(screen.getByText("acct-abc-123")).toBeInTheDocument();
  });

  it("shows the REAL workspace id once loaded, not the account id", async () => {
    // A-2026-07-01 finding A-3: the field labelled "Workspace ID" must display
    // the actual workspace id (workspaces.id), not the personal account id.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (typeof url === "string" && url.includes("/api/v1/workspace")) {
          return new Response(
            JSON.stringify({ id: "ws-1", name: "Acme", language: "en", safe_mode: true }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
      }),
    );
    render(<GeneralTab />);
    await waitFor(() => expect(screen.getByText("ws-1")).toBeInTheDocument());
    expect(screen.queryByText("acct-abc-123")).not.toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("does not render a danger zone in this lift", () => {
    render(<GeneralTab />);
    expect(screen.queryByText(/delete workspace/i)).not.toBeInTheDocument();
  });

  it("renders the GDPR legal-basis badge (display-only)", () => {
    render(<GeneralTab />);
    // The default basis for a workspace is 'contract' — the badge surfaces
    // that as a read-only marker until the founder-editable UI ships.
    expect(screen.getByText(/legal basis/i)).toBeInTheDocument();
  });
});

// L3 (#5) — Safe / Auto mode toggle, wired to PATCH /api/v1/workspace.
describe("General tab — Safe Mode (#5)", () => {
  function mockWorkspace(safeMode: boolean) {
    return vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (typeof url === "string" && url.includes("/api/v1/workspace")) {
        const body =
          method === "PATCH" ? (JSON.parse(init?.body as string) as { safe_mode?: boolean }) : {};
        const value = body.safe_mode ?? safeMode;
        return new Response(
          JSON.stringify({ id: "ws-1", name: "Acme", language: "en", safe_mode: value }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
    });
  }

  it("reflects the loaded mode (Safe selected) and switching to Auto PATCHes safe_mode=false", async () => {
    const fetchMock = mockWorkspace(true);
    vi.stubGlobal("fetch", fetchMock);

    render(<GeneralTab />);

    // Auto becomes selectable once the workspace loads.
    const auto = await screen.findByRole("radio", { name: /auto/i });
    await waitFor(() => expect(auto).not.toBeDisabled());
    await userEvent.click(auto);

    await waitFor(() => {
      const patch = fetchMock.mock.calls.find(
        (c) =>
          typeof c[0] === "string" &&
          (c[0] as string).includes("/api/v1/workspace") &&
          (c[1] as RequestInit)?.method === "PATCH",
      );
      expect(patch).toBeTruthy();
      const [, init] = patch as unknown as [string, RequestInit];
      expect(JSON.parse(init.body as string)).toEqual({ safe_mode: false });
    });
  });
});
