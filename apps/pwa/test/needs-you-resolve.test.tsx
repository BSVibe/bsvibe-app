/**
 * NeedsYou "Decide" interactions — a Safe-Mode item is actionable (Approve /
 * Deny); a canonicalization proposal stays read-only. Drives the real
 * approve/deny client against a mocked fetch and asserts in-flight, resolved,
 * and error states, plus the onResolved callback that re-reads the Brief.
 */

import NeedsYou from "@/components/brief/NeedsYou";
import type { NeedsYouItem } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const SAFEMODE_ITEM: NeedsYouItem = {
  id: "safemode-sm-1",
  productSlug: "delivery",
  question: "A delivery is held in Safe Mode — approve to send it out?",
  resolve: { kind: "safemode", itemId: "sm-1" },
};

const PROPOSAL_ITEM: NeedsYouItem = {
  id: "proposal-prop-1",
  productSlug: "knowledge",
  question: "Approve merge_notes on “notes/auth”?",
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

describe("NeedsYou (Decide interactions)", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders Approve + Deny only for a Safe-Mode item", () => {
    render(<NeedsYou items={[SAFEMODE_ITEM, PROPOSAL_ITEM]} />);

    // One actionable row → exactly one Approve and one Deny.
    expect(screen.getAllByRole("button", { name: "Approve" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Deny" })).toHaveLength(1);
    // The proposal row is still shown (read-only) — its question renders.
    expect(screen.getByText("Approve merge_notes on “notes/auth”?")).toBeInTheDocument();
  });

  it("Approve POSTs /approve, shows resolved, and fires onResolved", async () => {
    const fetchMock = okFetch({ item_id: "sm-1", status: "approved", dispatched: true });
    global.fetch = fetchMock as unknown as typeof fetch;
    const onResolved = vi.fn();

    render(<NeedsYou items={[SAFEMODE_ITEM]} onResolved={onResolved} />);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(screen.getByText("Approved. Sending it out.")).toBeInTheDocument();
    });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/safemode/sm-1/approve");
    expect(init.method).toBe("POST");
    await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1));
  });

  it("Deny POSTs /deny, shows resolved, and fires onResolved", async () => {
    const fetchMock = okFetch({ item_id: "sm-1", status: "denied", dispatched: false });
    global.fetch = fetchMock as unknown as typeof fetch;
    const onResolved = vi.fn();

    render(<NeedsYou items={[SAFEMODE_ITEM]} onResolved={onResolved} />);
    await userEvent.click(screen.getByRole("button", { name: "Deny" }));

    await waitFor(() => {
      expect(screen.getByText("Dismissed.")).toBeInTheDocument();
    });
    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/safemode/sm-1/deny");
    await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1));
  });

  it("shows a calm inline error when approve fails — strip does not crash", async () => {
    global.fetch = vi.fn(
      async () => new Response("boom", { status: 500 }),
    ) as unknown as typeof fetch;
    const onResolved = vi.fn();

    render(<NeedsYou items={[SAFEMODE_ITEM]} onResolved={onResolved} />);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(screen.getByText("Couldn’t do that. Please try again.")).toBeInTheDocument();
    });
    // The row is still here and re-actionable; no re-read fired.
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(onResolved).not.toHaveBeenCalled();
  });
});
