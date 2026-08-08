/**
 * Settings → Developer → personal access tokens.
 *
 * The PAT exists for clients that can't complete the browser sign-in (a remote
 * tunnel, SSH, a headless box, a scheduled job). Three properties matter enough
 * to pin here:
 *
 *  - the raw token is shown exactly once, with a warning that says so;
 *  - the listing never re-serves it;
 *  - revoking asks first, and a failed revoke says so instead of looking done.
 */

import DeveloperTab from "@/components/settings/DeveloperTab";
import type { Pat, PatCreated } from "@/lib/api/pats";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api/oauth-clients", () => ({
  listOAuthClients: vi.fn(),
  createOAuthClient: vi.fn(),
  deleteOAuthClient: vi.fn(),
}));

vi.mock("@/lib/api/pats", () => ({
  listPats: vi.fn(),
  createPat: vi.fn(),
  deletePat: vi.fn(),
}));

import { listOAuthClients } from "@/lib/api/oauth-clients";
import { createPat, deletePat, listPats } from "@/lib/api/pats";

const PAT: Pat = {
  id: "pat-1",
  name: "mac-mini",
  scope: ["mcp:read", "mcp:write"],
  issued_at: "2026-08-08T00:00:00Z",
  expires_at: null,
};

const CREATED: PatCreated = { ...PAT, token: "eyJhbGciOiJFUzI1NiJ9.payload.sig" };

describe("DeveloperTab — personal access tokens", () => {
  // Set in beforeEach, not in the vi.mock factory: restoreAllMocks() wipes a
  // factory-supplied mockResolvedValue, and the sibling OAuth-clients section
  // would then render `undefined.length` from the second test onward.
  beforeEach(() => {
    vi.mocked(listOAuthClients).mockResolvedValue([]);
  });
  afterEach(() => vi.restoreAllMocks());

  it("shows an empty state when there are no tokens", async () => {
    vi.mocked(listPats).mockResolvedValue([]);

    render(<DeveloperTab />);

    expect(await screen.findByText(/no personal access tokens yet/i)).toBeInTheDocument();
  });

  it("lists an existing token without ever rendering its value", async () => {
    vi.mocked(listPats).mockResolvedValue([PAT]);

    render(<DeveloperTab />);

    expect(await screen.findByText("mac-mini")).toBeInTheDocument();
    expect(screen.getByText(/never expires/i)).toBeInTheDocument();
    expect(screen.queryByText(/eyJhbGciOiJFUzI1NiJ9/)).not.toBeInTheDocument();
  });

  it("shows the raw token once, with a warning that it will not be shown again", async () => {
    vi.mocked(listPats).mockResolvedValue([]);
    vi.mocked(createPat).mockResolvedValue(CREATED);

    render(<DeveloperTab />);
    await screen.findByText(/no personal access tokens yet/i);

    await userEvent.click(screen.getByRole("button", { name: /new token/i }));
    await userEvent.type(screen.getByLabelText(/token name/i), "mac-mini");
    await userEvent.click(screen.getByRole("button", { name: /^create token$/i }));

    expect(await screen.findByText(CREATED.token)).toBeInTheDocument();
    expect(screen.getByText(/only time it.s shown/i)).toBeInTheDocument();

    // Dismissing the one-time panel returns to the list, which must not carry it.
    vi.mocked(listPats).mockResolvedValue([PAT]);
    await userEvent.click(screen.getByRole("button", { name: /^done$/i }));

    expect(await screen.findByText("mac-mini")).toBeInTheDocument();
    expect(screen.queryByText(CREATED.token)).not.toBeInTheDocument();
  });

  it("asks for confirmation before revoking", async () => {
    vi.mocked(listPats).mockResolvedValue([PAT]);
    vi.mocked(deletePat).mockResolvedValue(undefined);

    render(<DeveloperTab />);
    await screen.findByText("mac-mini");

    await userEvent.click(screen.getByRole("button", { name: /^revoke$/i }));
    // Nothing is destroyed on the first click.
    expect(deletePat).not.toHaveBeenCalled();
    expect(screen.getByText(/stops working immediately/i)).toBeInTheDocument();

    vi.mocked(listPats).mockResolvedValue([]);
    await userEvent.click(screen.getByRole("button", { name: /^confirm$/i }));

    expect(deletePat).toHaveBeenCalledWith("pat-1");
    expect(await screen.findByText(/no personal access tokens yet/i)).toBeInTheDocument();
  });

  it("surfaces an inline error when revoking fails", async () => {
    vi.mocked(listPats).mockResolvedValue([PAT]);
    vi.mocked(deletePat).mockRejectedValue(new Error("boom"));

    render(<DeveloperTab />);
    await screen.findByText("mac-mini");

    await userEvent.click(screen.getByRole("button", { name: /^revoke$/i }));
    await userEvent.click(screen.getByRole("button", { name: /^confirm$/i }));

    expect(await screen.findByText(/couldn.t revoke that token/i)).toBeInTheDocument();
  });
});
