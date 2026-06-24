/**
 * Delivery Report — true red/green diff render (Lift 2b).
 *
 * When the deliverable carries a captured `git diff` (product runs), the right
 * panel renders each file's real old↔new change: removed lines red, added lines
 * green, context plain — instead of content-as-additions. Files NOT in the diff
 * (or a deliverable with no captured diff at all) fall back to the Lift 1
 * additions render (fetched file content).
 */

import DeliveryReport from "@/components/deliverables/DeliveryReport";
import type { DeliverableReport } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
    deliverable_type: "code",
    summary: "Tweak blog helpers",
    artifact_refs: ["src/blog.ts", "tests/blog.test.ts"],
    artifact_uri: null,
    diff_url: null,
    verified: true,
    created_at: NOW,
  },
  request: null,
  verified: true,
  verifications: [],
  references: [],
};

// A unified diff that touches ONLY src/blog.ts (a modification).
const DIFF = `diff --git a/src/blog.ts b/src/blog.ts
--- a/src/blog.ts
+++ b/src/blog.ts
@@ -1,1 +1,1 @@
-export const old = 1
+export const next = 2
`;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installFetch(opts?: {
  diff?: () => unknown;
  artifact?: (url: string) => Response;
}) {
  const diffFn = opts?.diff ?? (() => ({ diff: DIFF, truncated: false }));
  const artifactFn =
    opts?.artifact ??
    ((url: string) =>
      json({
        ref: url.includes("tests/blog.test.ts") ? "tests/blog.test.ts" : "src/blog.ts",
        content: "test('related', () => {})\n",
        truncated: false,
        binary: false,
      }));
  global.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.includes("/diff")) return json(diffFn());
    if (url.includes("/artifacts/")) return artifactFn(url);
    return json(REPORT);
  }) as unknown as typeof fetch;
}

describe("Delivery Report — red/green diff render", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders removed lines red and added lines green for a file in the diff", async () => {
    installFetch();
    const { container } = render(<DeliveryReport deliverableId="d1" />);

    const built = await screen.findByRole("region", { name: /what was built/i });
    await waitFor(() => {
      expect(within(built).getByText("export const next = 2")).toBeInTheDocument();
    });
    const del = container.querySelector(".report-diff__line--del");
    const add = container.querySelector(".report-diff__line--add");
    expect(del?.textContent).toContain("export const old = 1");
    expect(add?.textContent).toContain("export const next = 2");
    // The +N / −N change counts surface in the panel head.
    expect(container.querySelector(".report-diff__added")?.textContent).toContain("+1");
    expect(container.querySelector(".report-diff__removed")?.textContent).toContain("-1");
  });

  it("falls back to additions for a file NOT present in the diff", async () => {
    installFetch();
    render(<DeliveryReport deliverableId="d1" />);

    const built = await screen.findByRole("region", { name: /what was built/i });
    const files = within(built).getByRole("navigation", { name: /files/i });
    // tests/blog.test.ts is not in the diff → selecting it fetches content.
    await userEvent.click(within(files).getByRole("button", { name: /tests\/blog\.test\.ts/ }));
    await waitFor(() => {
      expect(within(built).getByText(/test\('related'/)).toBeInTheDocument();
    });
  });

  it("falls back to additions entirely when no diff was captured (diff: null)", async () => {
    installFetch({ diff: () => ({ diff: null, truncated: false }) });
    const { container } = render(<DeliveryReport deliverableId="d1" />);

    const built = await screen.findByRole("region", { name: /what was built/i });
    await waitFor(() => {
      expect(within(built).getByText(/test\('related'/)).toBeInTheDocument();
    });
    // No removed (red) rows when rendering as additions.
    expect(container.querySelector(".report-diff__line--del")).toBeNull();
  });
});
