/**
 * DeveloperTab → ClientRow revoke — a failed revoke must surface an inline
 * error, not fail silently (a silently-failed revoke reads as "done" and the
 * founder walks away thinking a live client credential is dead when it isn't).
 */

import DeveloperTab from "@/components/settings/DeveloperTab";
import type { OAuthClient } from "@/lib/api/oauth-clients";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api/oauth-clients", () => ({
  listOAuthClients: vi.fn(),
  createOAuthClient: vi.fn(),
  deleteOAuthClient: vi.fn(),
}));

import { deleteOAuthClient, listOAuthClients } from "@/lib/api/oauth-clients";

const CLIENT: OAuthClient = {
  id: "row-1",
  client_id: "cid-1",
  client_name: "Claude Code",
  client_type: "public",
  redirect_uris: ["http://localhost/cb"],
  allowed_scopes: ["mcp:read"],
  created_at: "2026-05-01T00:00:00Z",
  revoked_at: null,
};

describe("DeveloperTab — revoke error", () => {
  afterEach(() => vi.restoreAllMocks());

  it("surfaces an inline error when revoking a client fails", async () => {
    vi.mocked(listOAuthClients).mockResolvedValue([CLIENT]);
    vi.mocked(deleteOAuthClient).mockRejectedValue(new Error("boom"));

    render(<DeveloperTab />);
    await screen.findByText("Claude Code");
    await userEvent.click(screen.getByRole("button", { name: /^Revoke$/i }));

    expect(await screen.findByText(/couldn.t revoke that client/i)).toBeInTheDocument();
  });
});
