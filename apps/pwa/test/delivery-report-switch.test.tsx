/**
 * Delivery Report — seamless file switching (#4 polish).
 *
 * When switching to another file whose content must be fetched (a no-diff file:
 * Direct / additions / markdown), the panel keeps showing the PREVIOUS file's
 * content until the next one arrives — it does not blank to a loading note (and
 * so does not reflow). Only the very first load (nothing to show yet) shows the
 * loading note.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@git-diff-view/react", () => {
  const React = require("react");
  return {
    DiffModeEnum: { SplitGitHub: 1, SplitGitLab: 2, Split: 3, Unified: 4 },
    DiffView: ({ data }: { data?: { newFile?: { fileName?: string | null }; hunks?: string[] } }) =>
      React.createElement(
        "div",
        { "data-testid": "git-diff-view", "data-filename": data?.newFile?.fileName ?? "" },
        React.createElement("pre", null, (data?.hunks ?? []).join("\n")),
      ),
  };
});

import DeliveryReport from "@/components/deliverables/DeliveryReport";
import type { DeliverableReport } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const NOW = "2026-05-23T00:00:00Z";

const REPORT: DeliverableReport = {
  deliverable: {
    id: "d1",
    run_id: "r1",
    workspace_id: "ws-1",
    deliverable_type: "direct_output",
    summary: "Two notes",
    artifact_refs: ["a.txt", "b.txt"],
    artifact_uri: null,
    diff_url: null,
    verified: true,
    created_at: NOW,
  },
  request: null,
  verified: true,
  verifications: [],
  references: [],
  narrative: null,
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function panel() {
  return document.querySelector('[data-testid="git-diff-view"]');
}

describe("Delivery Report — seamless file switching", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("keeps the previous file visible while the next one loads (no blank)", async () => {
    // b.txt's fetch is deferred so we can observe the in-flight switch. The
    // Promise executor runs synchronously, so resolveB is assigned before use.
    let resolveB!: (r: Response) => void;
    const bPending = new Promise<Response>((res) => {
      resolveB = res;
    });
    global.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/diff")) return json({ diff: null, truncated: false });
      if (url.includes("a.txt"))
        return json({ ref: "a.txt", content: "alpha content\n", truncated: false, binary: false });
      if (url.includes("b.txt")) return bPending;
      return json(REPORT);
    }) as unknown as typeof fetch;

    render(<DeliveryReport deliverableId="d1" />);
    const built = await screen.findByRole("region", { name: /what was built/i });
    // a.txt loads first.
    await waitFor(() => expect(panel()?.textContent).toContain("+alpha content"));

    // Switch to b.txt — its fetch is still pending.
    await userEvent.click(within(built).getByRole("button", { name: /b\.txt/ }));

    // The panel still shows a.txt (NOT a blank loading note).
    expect(panel()?.textContent).toContain("+alpha content");
    expect(screen.queryByText(/opening this file/i)).toBeNull();

    // Once b.txt resolves, it swaps in.
    resolveB(json({ ref: "b.txt", content: "bravo content\n", truncated: false, binary: false }));
    await waitFor(() => expect(panel()?.textContent).toContain("+bravo content"));
    expect(panel()?.textContent).not.toContain("+alpha content");
  });

  it("shows the loading note only on the first load (nothing to keep yet)", async () => {
    let resolveA!: (r: Response) => void;
    const aPending = new Promise<Response>((res) => {
      resolveA = res;
    });
    global.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/diff")) return json({ diff: null, truncated: false });
      if (url.includes("a.txt")) return aPending;
      return json(REPORT);
    }) as unknown as typeof fetch;

    render(<DeliveryReport deliverableId="d1" />);
    await screen.findByRole("region", { name: /what was built/i });
    // Nothing shown yet → the loading note appears.
    await waitFor(() => expect(screen.getByText(/opening this file/i)).toBeInTheDocument());
    resolveA(json({ ref: "a.txt", content: "alpha\n", truncated: false, binary: false }));
    await waitFor(() => expect(panel()?.textContent).toContain("+alpha"));
  });
});
