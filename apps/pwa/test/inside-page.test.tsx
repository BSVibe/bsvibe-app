/**
 * Inside surface — the read-only knowledge snapshot container, driven by a
 * route-aware mocked fetch. Asserts:
 *  - both sections render the REAL fields (concept name + summary + alias count;
 *    observation title + excerpt + tags)
 *  - the calm empty state when the workspace has learned nothing yet
 *  - a calm inline error (not a blank page) when a section's read fails — the
 *    other section still renders
 *  - a loading note before the reads land
 */

import Inside from "@/components/inside/Inside";
import type { Concept, Observation } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const CONCEPT: Concept = {
  id: "self-hosting",
  name: "Self-hosting",
  summary: "Running services on owned hardware instead of a managed cloud.",
  aliases: ["self host", "selfhosting"],
  alias_count: 2,
  created_at: "2026-05-22T00:00:00Z",
  updated_at: "2026-05-23T00:00:00Z",
};

const OBSERVATION: Observation = {
  id: "garden/seedling/2026-05-23-related-posts.md",
  title: "Related posts widget shows 5 items",
  excerpt: "Founder settled on 5 over 3; both fit the layout.",
  tags: ["frontend", "widget"],
  captured_at: "2026-05-23T00:00:00Z",
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * A route-aware fetch mock. `concepts` / `observations` return each list GET's
 * contents (or a Response to force a failure).
 */
function installFetch(opts: {
  concepts: () => Concept[] | Response;
  observations: () => Observation[] | Response;
}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/v1/inside/concepts")) {
      const c = opts.concepts();
      return c instanceof Response ? c : json(c);
    }
    if (url.startsWith("/api/v1/inside/observations")) {
      const o = opts.observations();
      return o instanceof Response ? o : json(o);
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  global.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

describe("Inside surface", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders both sections from the two lists with their real fields", async () => {
    installFetch({ concepts: () => [CONCEPT], observations: () => [OBSERVATION] });

    render(<Inside />);

    await waitFor(() => {
      expect(screen.getByText(CONCEPT.name)).toBeInTheDocument();
    });
    expect(screen.getByRole("region", { name: "What I know" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Recently observed" })).toBeInTheDocument();

    // Concept fields: name, summary, alias count surfaced in plain language.
    expect(screen.getByText(CONCEPT.summary)).toBeInTheDocument();
    expect(screen.getByText(/2 mentions/)).toBeInTheDocument();

    // Observation fields: title, excerpt, tags.
    expect(screen.getByText(OBSERVATION.title)).toBeInTheDocument();
    expect(screen.getByText(OBSERVATION.excerpt)).toBeInTheDocument();
    expect(screen.getByText("frontend")).toBeInTheDocument();
    expect(screen.getByText("widget")).toBeInTheDocument();
  });

  it("shows the calm empty state when nothing has been learned yet", async () => {
    installFetch({ concepts: () => [], observations: () => [] });

    render(<Inside />);

    await waitFor(() => {
      expect(screen.getByText(/I haven’t learned anything yet/)).toBeInTheDocument();
    });
    expect(screen.queryByRole("region", { name: "What I know" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Recently observed" })).not.toBeInTheDocument();
  });

  it("renders concepts even when observations fail — calm, not blank", async () => {
    installFetch({
      concepts: () => [CONCEPT],
      observations: () => json("boom", 500),
    });

    render(<Inside />);

    await waitFor(() => {
      expect(screen.getByText(CONCEPT.name)).toBeInTheDocument();
    });
    // The failing section degrades to an inline note, not a blanked page.
    expect(screen.getByRole("region", { name: "What I know" })).toBeInTheDocument();
    expect(screen.getByText(/Couldn’t load recent observations/)).toBeInTheDocument();
  });

  it("renders observations even when concepts fail — calm, not blank", async () => {
    installFetch({
      concepts: () => json("forbidden", 403),
      observations: () => [OBSERVATION],
    });

    render(<Inside />);

    await waitFor(() => {
      expect(screen.getByText(OBSERVATION.title)).toBeInTheDocument();
    });
    expect(screen.getByRole("region", { name: "Recently observed" })).toBeInTheDocument();
    expect(screen.getByText(/Couldn’t load what I know/)).toBeInTheDocument();
  });

  it("shows a loading note before the reads land", async () => {
    installFetch({ concepts: () => [CONCEPT], observations: () => [OBSERVATION] });

    render(<Inside />);

    // The loading note is visible synchronously, before the lists resolve…
    expect(screen.getByText(/Looking at what I know/)).toBeInTheDocument();
    // …then the resolved content replaces it (waited so the state update flushes
    // inside act, no dangling update warning).
    await waitFor(() => {
      expect(screen.getByText(CONCEPT.name)).toBeInTheDocument();
    });
  });

  it("renders a freshly-promoted anchor that carries no summary", async () => {
    const bare: Concept = { ...CONCEPT, summary: "", aliases: [], alias_count: 0 };
    installFetch({ concepts: () => [bare], observations: () => [] });

    render(<Inside />);

    await waitFor(() => {
      expect(screen.getByText(bare.name)).toBeInTheDocument();
    });
    // No alias count chip when there are no aliases.
    expect(screen.queryByText(/mentions/)).not.toBeInTheDocument();
  });
});
