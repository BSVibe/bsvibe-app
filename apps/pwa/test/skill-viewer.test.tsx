/**
 * Skill viewer surface — the single-skill detail container, driven by a mocked
 * fetch (GET /api/v1/skills/{name}). Asserts:
 *  - renders the skill's real manifest fields (name, version, description,
 *    author, allowed tools, system-prompt body)
 *  - the authoring affordance ("Edit") is ENABLED and opens the inline editor
 *    (prefilled summary + system prompt)
 *  - the calm not-found state for an unknown skill (404 → not-found, with a way
 *    back to the library)
 *  - a calm inline error (not a blank page) when the read otherwise fails
 *  - a loading note before the read lands
 */

import SkillViewer from "@/components/skills/SkillViewer";
import type { Skill } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  usePathname: () => "/skills/blog-writer",
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
}));

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const SKILL: Skill = {
  name: "blog-writer",
  version: "1.0.0",
  description: "Drafts a technical blog post in the house voice.",
  author: "founder",
  allowed_tools: ["read", "write"],
  model: "claude-opus",
  has_system_prompt: true,
  system_prompt: "You write calm, precise technical prose.",
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installFetch(skill: () => Skill | Response) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/v1/skills/")) {
      const s = skill();
      return s instanceof Response ? s : json(s);
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  global.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

describe("Skill viewer surface", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the skill's manifest fields", async () => {
    installFetch(() => SKILL);

    render(<SkillViewer name="blog-writer" />);

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: SKILL.name })).toBeInTheDocument();
    });
    expect(screen.getByText(SKILL.description)).toBeInTheDocument();
    expect(screen.getByText(/1\.0\.0/)).toBeInTheDocument();
    expect(screen.getByText(/founder/)).toBeInTheDocument();
    expect(screen.getByText("read")).toBeInTheDocument();
    expect(screen.getByText("write")).toBeInTheDocument();
  });

  it("renders an ENABLED 'Edit' affordance that opens the inline editor", async () => {
    installFetch(() => SKILL);

    render(<SkillViewer name="blog-writer" />);

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: SKILL.name })).toBeInTheDocument();
    });
    const edit = screen.getByRole("button", { name: /^Edit$/i });
    expect(edit).not.toBeDisabled();

    await userEvent.click(edit);
    // The editor opens with the summary + system prompt prefilled.
    expect(screen.getByLabelText(/Summary/i)).toHaveValue(SKILL.description);
    expect(screen.getByLabelText(/System prompt/i)).toHaveValue(SKILL.system_prompt);
  });

  it("shows the calm not-found state for an unknown skill (404)", async () => {
    installFetch(() => json("not found", 404));

    render(<SkillViewer name="nope" />);

    await waitFor(() => {
      expect(screen.getByText(/I don’t know that skill/)).toBeInTheDocument();
    });
    // A way back to the library (the inline "Back to Skills" link).
    expect(screen.getByRole("link", { name: /Back to Skills/ })).toHaveAttribute("href", "/skills");
  });

  it("shows a calm inline error (not a blank page) on a non-404 failure", async () => {
    installFetch(() => json("boom", 500));

    render(<SkillViewer name="blog-writer" />);

    await waitFor(() => {
      expect(screen.getByText(/Couldn’t load this skill/)).toBeInTheDocument();
    });
  });

  it("shows a loading note before the read lands", async () => {
    installFetch(() => SKILL);

    render(<SkillViewer name="blog-writer" />);

    expect(screen.getByText(/Looking at this skill/)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: SKILL.name })).toBeInTheDocument();
    });
  });
});
