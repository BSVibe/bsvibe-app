/**
 * The free plan's concurrent-run cap, as the founder actually meets it.
 *
 * The backend refuses an over-budget submit with 429. Before this, the overlay
 * keyed its only actionable hint off the *status code* — `err.status === 400`
 * meant "this workspace has no products" — so any second reason to refuse
 * would have been shown to a founder as "create a product first". These lock
 * the two refusals apart, and keep the number in the sentence coming from the
 * response rather than from a hardcoded 3 that would lie for a workspace on a
 * different cap.
 */

import { DirectOverlay } from "@/components/shell/DirectAction";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  usePathname: () => "/brief",
}));

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

/** Answer `/api/v1/messages` with `status`+`body`; everything else 202-accepts
 *  (the overlay also asks `/messages/ask` and reads `/api/v1/products`). */
function mockSubmit(status: number, body: unknown) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.endsWith("/api/v1/messages") && init?.method === "POST") {
      return new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response(JSON.stringify({ accepted: true, duplicate: false }), {
      status: 202,
      headers: { "Content-Type": "application/json" },
    });
  });
}

async function submit(text: string) {
  render(<DirectOverlay open onClose={() => {}} />);
  await userEvent.type(screen.getByRole("textbox"), text);
  fireEvent.click(screen.getByRole("button", { name: "Request" }));
}

describe("Direct compose — the concurrent-run cap", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("names the founder's actual limit when the plan refuses the submit", async () => {
    global.fetch = mockSubmit(429, {
      detail: { code: "run_cap_reached", limit: 3, held: 3 },
    }) as unknown as typeof fetch;

    await submit("one too many");

    await waitFor(() => {
      expect(screen.getByText(/3 requests/i)).toBeInTheDocument();
    });
  });

  it("does not tell a founder who has products to create one", async () => {
    // The regression this file exists for: a 429 falling through the old
    // status-keyed branch would have shown the zero-product hint.
    global.fetch = mockSubmit(429, {
      detail: { code: "run_cap_reached", limit: 3, held: 3 },
    }) as unknown as typeof fetch;

    await submit("one too many");

    await waitFor(() => {
      expect(screen.getByText(/requests/i)).toBeInTheDocument();
    });
    expect(screen.queryByText(/Create a product first/i)).not.toBeInTheDocument();
  });

  it("takes the number from the response, not from a hardcoded free-plan 3", async () => {
    global.fetch = mockSubmit(429, {
      detail: { code: "run_cap_reached", limit: 10, held: 10 },
    }) as unknown as typeof fetch;

    await submit("a workspace on a different cap");

    await waitFor(() => {
      expect(screen.getByText(/10 requests/i)).toBeInTheDocument();
    });
  });

  it("points at the plans page", async () => {
    global.fetch = mockSubmit(429, {
      detail: { code: "run_cap_reached", limit: 3, held: 3 },
    }) as unknown as typeof fetch;

    await submit("where do I go from here");

    const link = await screen.findByRole("link", { name: /plans/i });
    expect(link).toHaveAttribute("href", "https://bsvibe.dev/en/pricing");
  });

  it("still shows the zero-product hint on a 400 (the control)", async () => {
    // Without this, deleting the 400 branch entirely would leave every test
    // above green.
    global.fetch = mockSubmit(400, {
      detail: "workspace has no products — create one before submitting a message",
    }) as unknown as typeof fetch;

    await submit("no products yet");

    await waitFor(() => {
      expect(screen.getByText(/Create a product first/i)).toBeInTheDocument();
    });
  });
});
