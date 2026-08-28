/**
 * Settings → Developer → rebuild the search index.
 *
 * The vector index is populated event-driven (the settle promote hook), so it
 * only self-heals when knowledge activity happens to occur. Measured on prod
 * 2026-08-28: 1,724 rows carrying no content fingerprint and no run in 30 hours
 * to trigger a pass. The backfill existed and was correct — it had no surface a
 * founder could press. This is that surface.
 *
 * What is pinned here is the honesty of the report, not the layout:
 *  - real counts come back, not a generic "done";
 *  - "no embedding model configured" is NOT rendered as "0 notes needed work";
 *  - a failure says so instead of looking finished;
 *  - the button cannot be double-fired while a pass is in flight.
 */

import DeveloperTab from "@/components/settings/DeveloperTab";
import type { ReindexResult } from "@/lib/api/types";
import { act, render, screen } from "@testing-library/react";
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

vi.mock("@/lib/api/knowledge", () => ({
  reindexEmbeddings: vi.fn(),
}));

import { reindexEmbeddings } from "@/lib/api/knowledge";
import { listOAuthClients } from "@/lib/api/oauth-clients";
import { listPats } from "@/lib/api/pats";

const RESULT: ReindexResult = {
  scanned: 1685,
  embedded: 1685,
  already: 0,
  disabled: false,
  remaining: 0,
  removed: 0,
};

describe("DeveloperTab — rebuild the search index", () => {
  // Set here, not in the vi.mock factory: restoreAllMocks() wipes a
  // factory-supplied mockResolvedValue and the sibling sections would then
  // render from `undefined`.
  beforeEach(() => {
    vi.mocked(listOAuthClients).mockResolvedValue([]);
    vi.mocked(listPats).mockResolvedValue([]);
  });
  afterEach(() => vi.restoreAllMocks());

  it("reports the real counts after a pass", async () => {
    vi.mocked(reindexEmbeddings).mockResolvedValue(RESULT);

    render(<DeveloperTab />);
    await userEvent.click(await screen.findByRole("button", { name: /rebuild index/i }));

    expect(reindexEmbeddings).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/1685 embedded/i)).toBeInTheDocument();
    expect(screen.getByText(/1685 scanned/i)).toBeInTheDocument();
  });

  it("says the deployment has no embedding model instead of showing zeros", async () => {
    vi.mocked(reindexEmbeddings).mockResolvedValue({
      scanned: 0,
      embedded: 0,
      already: 0,
      disabled: true,
      remaining: 0,
      removed: 0,
    });

    render(<DeveloperTab />);
    await userEvent.click(await screen.findByRole("button", { name: /rebuild index/i }));

    expect(await screen.findByText(/no embedding model is configured/i)).toBeInTheDocument();
    expect(screen.queryByText(/0 embedded/i)).not.toBeInTheDocument();
  });

  it("surfaces a failure instead of looking done", async () => {
    vi.mocked(reindexEmbeddings).mockRejectedValue(new Error("boom"));

    render(<DeveloperTab />);
    await userEvent.click(await screen.findByRole("button", { name: /rebuild index/i }));

    expect(await screen.findByText(/boom/i)).toBeInTheDocument();
    expect(screen.queryByText(/embedded/i)).not.toBeInTheDocument();
  });

  it("cannot be fired twice while a pass is in flight", async () => {
    let release: (r: ReindexResult) => void = () => {};
    vi.mocked(reindexEmbeddings).mockReturnValue(
      new Promise<ReindexResult>((resolve) => {
        release = resolve;
      }),
    );

    render(<DeveloperTab />);
    const button = await screen.findByRole("button", { name: /rebuild index/i });
    await userEvent.click(button);

    expect(await screen.findByRole("button", { name: /rebuilding/i })).toBeDisabled();
    await userEvent.click(button);
    expect(reindexEmbeddings).toHaveBeenCalledTimes(1);

    release(RESULT);
  });

  it("survives a double-click dispatched before the button can re-render", async () => {
    // The `disabled` attribute only exists AFTER React re-renders, so it cannot
    // stop a second click dispatched in the same tick — a real double-click on
    // an expensive pass. Both clicks are dispatched inside one `act` here, which
    // is precisely the window the attribute does not cover; what holds the line
    // is the in-flight state guard inside the handler.
    let release: (r: ReindexResult) => void = () => {};
    vi.mocked(reindexEmbeddings).mockReturnValue(
      new Promise<ReindexResult>((resolve) => {
        release = resolve;
      }),
    );

    render(<DeveloperTab />);
    const button = await screen.findByRole("button", { name: /rebuild index/i });

    await act(async () => {
      button.click();
      button.click();
    });

    expect(reindexEmbeddings).toHaveBeenCalledTimes(1);

    release(RESULT);
  });
});

describe("DeveloperTab — a bounded pass keeps going until it is done", () => {
  beforeEach(() => {
    vi.mocked(listOAuthClients).mockResolvedValue([]);
    vi.mocked(listPats).mockResolvedValue([]);
  });
  afterEach(() => vi.restoreAllMocks());

  it("keeps calling while work remains and reports the TOTAL, not the last pass", async () => {
    // One pass is capped so the request answers before the proxy gives up.
    // If the button fired once and stopped, the founder would see "100 embedded"
    // over a corpus of 250 and believe the index was rebuilt.
    vi.mocked(reindexEmbeddings)
      .mockResolvedValueOnce({
        scanned: 100,
        embedded: 100,
        already: 0,
        disabled: false,
        remaining: 150,
        removed: 0,
      })
      .mockResolvedValueOnce({
        scanned: 100,
        embedded: 100,
        already: 0,
        disabled: false,
        remaining: 50,
        removed: 0,
      })
      .mockResolvedValueOnce({
        scanned: 50,
        embedded: 50,
        already: 0,
        disabled: false,
        remaining: 0,
        removed: 0,
      });

    render(<DeveloperTab />);
    await userEvent.click(await screen.findByRole("button", { name: /rebuild index/i }));

    expect(await screen.findByText(/250 embedded/i)).toBeInTheDocument();
    expect(reindexEmbeddings).toHaveBeenCalledTimes(3);
  });

  it("gives up rather than spinning when the server never stops saying 'more'", async () => {
    // The loop is driven by a number the SERVER supplies. A stuck or buggy
    // backend that always reports work remaining must not spin the founder's
    // browser forever — it stops and says what it did.
    vi.mocked(reindexEmbeddings).mockResolvedValue({
      scanned: 100,
      embedded: 100,
      already: 0,
      disabled: false,
      remaining: 999,
      removed: 0,
    });

    render(<DeveloperTab />);
    await userEvent.click(await screen.findByRole("button", { name: /rebuild index/i }));

    expect(await screen.findByText(/still had work left/i)).toBeInTheDocument();
    expect(vi.mocked(reindexEmbeddings).mock.calls.length).toBeLessThanOrEqual(100);
  });

  it("stops and reports the error if a later pass fails", async () => {
    vi.mocked(reindexEmbeddings)
      .mockResolvedValueOnce({
        scanned: 100,
        embedded: 100,
        already: 0,
        disabled: false,
        remaining: 150,
        removed: 0,
      })
      .mockRejectedValueOnce(new Error("gateway timeout"));

    render(<DeveloperTab />);
    await userEvent.click(await screen.findByRole("button", { name: /rebuild index/i }));

    expect(await screen.findByText(/gateway timeout/i)).toBeInTheDocument();
    expect(reindexEmbeddings).toHaveBeenCalledTimes(2);
  });
});
