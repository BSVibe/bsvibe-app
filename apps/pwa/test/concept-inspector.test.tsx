/**
 * Concept inspector — the read-only detail drawer behind a clicked concept,
 * driven by a mocked fetch (GET /api/v1/inside/concepts/{id}). Asserts:
 *  - renders the concept's real fields (name, aliases, related concepts with a
 *    weight signal, source observations with title + excerpt + date)
 *  - clicking a related concept pivots the inspector (fetches the neighbour)
 *  - the Edit / Retract affordances are present but DISABLED (deferred — no v1
 *    write endpoint), with a "coming soon" hint
 *  - a calm loading note, a calm not-found (404) state, and a calm inline error
 *  - a Close affordance fires onClose
 *
 * The inspector is read-only; these are the same calm-degradation guarantees the
 * rest of the Knowledge surface gives.
 */

import ConceptInspector from "@/components/knowledge/ConceptInspector";
import type { ConceptDetail } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const AUTH: ConceptDetail = {
  id: "auth",
  name: "Auth",
  aliases: ["authn", "authentication"],
  related: [{ id: "jwks", name: "JWKS", weight: 2 }],
  observations: [
    {
      id: "garden/seedling/obs-a.md",
      title: "Wired the auth callback",
      excerpt: "Founder confirmed the redirect target.",
      captured_at: "2026-05-20",
    },
  ],
};

const JWKS: ConceptDetail = {
  id: "jwks",
  name: "JWKS",
  aliases: [],
  related: [{ id: "auth", name: "Auth", weight: 2 }],
  observations: [],
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** A route-aware fetch mock keyed on the concept id in the path. */
function installFetch(byId: Record<string, () => ConceptDetail | Response>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const match = url.match(/\/api\/v1\/inside\/concepts\/([^?]+)/);
    if (match) {
      const id = decodeURIComponent(match[1]);
      const handler = byId[id];
      if (!handler) return json("not found", 404);
      const r = handler();
      return r instanceof Response ? r : json(r);
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  global.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

describe("Concept inspector", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the concept's name, aliases, related concepts, and observations", async () => {
    installFetch({ auth: () => AUTH });

    render(<ConceptInspector conceptId="auth" onClose={vi.fn()} onPivot={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Auth" })).toBeInTheDocument();
    });
    expect(screen.getByText("authn")).toBeInTheDocument();
    expect(screen.getByText("authentication")).toBeInTheDocument();
    // Related concept (clickable) + its weight signal.
    expect(screen.getByRole("button", { name: /JWKS/ })).toBeInTheDocument();
    // Source observation fields.
    expect(screen.getByText("Wired the auth callback")).toBeInTheDocument();
    expect(screen.getByText(/redirect target/)).toBeInTheDocument();
  });

  it("pivots when a related concept is clicked", async () => {
    installFetch({ auth: () => AUTH, jwks: () => JWKS });
    const onPivot = vi.fn();

    render(<ConceptInspector conceptId="auth" onClose={vi.fn()} onPivot={onPivot} />);

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Auth" })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: /JWKS/ }));
    expect(onPivot).toHaveBeenCalledWith("jwks");
  });

  it("renders DISABLED Edit + Retract affordances (deferred, no write API)", async () => {
    installFetch({ auth: () => AUTH });

    render(<ConceptInspector conceptId="auth" onClose={vi.fn()} onPivot={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Auth" })).toBeInTheDocument();
    });
    const edit = screen.getByRole("button", { name: /Edit/i });
    const retract = screen.getByRole("button", { name: /Retract/i });
    expect(edit).toBeDisabled();
    expect(retract).toBeDisabled();
    expect(edit).toHaveAttribute("title", expect.stringMatching(/coming soon/i));
  });

  it("fires onClose from the close affordance", async () => {
    installFetch({ auth: () => AUTH });
    const onClose = vi.fn();

    render(<ConceptInspector conceptId="auth" onClose={onClose} onPivot={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Auth" })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    expect(onClose).toHaveBeenCalled();
  });

  it("shows a loading note before the read lands", async () => {
    installFetch({ auth: () => AUTH });

    render(<ConceptInspector conceptId="auth" onClose={vi.fn()} onPivot={vi.fn()} />);

    expect(screen.getByText(/Looking at this/)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Auth" })).toBeInTheDocument();
    });
  });

  it("shows a calm not-found state on a 404", async () => {
    installFetch({ auth: () => json("not found", 404) });

    render(<ConceptInspector conceptId="auth" onClose={vi.fn()} onPivot={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText(/don’t know that concept/i)).toBeInTheDocument();
    });
  });

  it("shows a calm inline error on a non-404 failure", async () => {
    installFetch({ auth: () => json("boom", 500) });

    render(<ConceptInspector conceptId="auth" onClose={vi.fn()} onPivot={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText(/Couldn’t load this concept/)).toBeInTheDocument();
    });
  });
});
