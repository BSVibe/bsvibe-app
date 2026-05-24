/**
 * Skills Library surface — the read-only skill list container, driven by a
 * mocked fetch (GET /api/v1/skills). Asserts:
 *  - renders a card per skill with its real fields (name + description, and a
 *    "model" / system-prompt hint when present), each linking to its viewer
 *  - the calm empty state when the workspace has no skills yet
 *  - a calm inline error (not a blank page / not a crash) when the read fails
 *  - a loading note before the read lands
 *  - the authoring affordance ("New skill") is now ENABLED → opens a create form;
 *    submitting fires createSkill with the body, then refreshes the list; a
 *    duplicate (409) shows a calm inline error and the form stays usable; the
 *    form validates required fields before firing the request.
 */

import SkillsLibrary from "@/components/skills/SkillsLibrary";
import type { Skill } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  usePathname: () => "/skills",
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
}));

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const BLOG_WRITER: Skill = {
  name: "blog-writer",
  version: "1.0.0",
  description: "Drafts a technical blog post in the house voice.",
  author: "founder",
  allowed_tools: ["read", "write"],
  model: "claude-opus",
  has_system_prompt: true,
  system_prompt: "You write calm, precise technical prose.",
};

const RELEASE_NOTES: Skill = {
  name: "release-notes",
  version: "0.2.0",
  description: "Summarises merged PRs into a release note.",
  author: "",
  allowed_tools: [],
  model: null,
  has_system_prompt: false,
  system_prompt: "",
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installFetch(skills: () => Skill[] | Response) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/v1/skills")) {
      const s = skills();
      return s instanceof Response ? s : json(s);
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  global.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

/** Fetch mock that routes GET (list) and POST (create) separately so the
 *  create flow can be exercised end-to-end. `created` is the 201 row the POST
 *  returns; once created it is appended to subsequent list reads. */
function installCrudFetch(initial: Skill[], postStatus = 201) {
  let rows = [...initial];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (!url.startsWith("/api/v1/skills")) throw new Error(`unexpected fetch ${url}`);
    const method = (init?.method ?? "GET").toUpperCase();
    if (method === "POST") {
      if (postStatus !== 201) return json("conflict", postStatus);
      const body = JSON.parse(init?.body as string) as {
        name: string;
        summary: string;
        system_prompt: string;
      };
      const row: Skill = {
        name: body.name,
        version: "1.0.0",
        description: body.summary,
        author: "",
        allowed_tools: [],
        model: null,
        has_system_prompt: true,
        system_prompt: body.system_prompt,
      };
      rows = [...rows, row];
      return json(row, 201);
    }
    return json(rows);
  });
  global.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

describe("Skills Library surface", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders a card per skill with name + description, linking to the viewer", async () => {
    installFetch(() => [BLOG_WRITER, RELEASE_NOTES]);

    render(<SkillsLibrary />);

    await waitFor(() => {
      expect(screen.getByText(BLOG_WRITER.name)).toBeInTheDocument();
    });
    expect(screen.getByText(BLOG_WRITER.description)).toBeInTheDocument();
    expect(screen.getByText(RELEASE_NOTES.name)).toBeInTheDocument();
    expect(screen.getByText(RELEASE_NOTES.description)).toBeInTheDocument();

    // Each card is a link to its detail route.
    const link = screen.getByRole("link", { name: /blog-writer/ });
    expect(link).toHaveAttribute("href", "/skills/blog-writer");
  });

  it("shows the calm empty state when there are no skills yet", async () => {
    installFetch(() => []);

    render(<SkillsLibrary />);

    await waitFor(() => {
      expect(screen.getByText(/No skills yet/i)).toBeInTheDocument();
    });
  });

  it("shows a calm inline note (not a crash) when the read fails", async () => {
    installFetch(() => json("boom", 500));

    render(<SkillsLibrary />);

    await waitFor(() => {
      expect(screen.getByText(/Couldn’t load skills/)).toBeInTheDocument();
    });
    // No skill cards rendered, but the page did not crash.
    expect(screen.queryByText(BLOG_WRITER.name)).not.toBeInTheDocument();
  });

  it("shows a loading note before the read lands", async () => {
    installFetch(() => [BLOG_WRITER]);

    render(<SkillsLibrary />);

    expect(screen.getByText(/Looking at your skills/)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText(BLOG_WRITER.name)).toBeInTheDocument();
    });
  });

  it("renders an ENABLED 'New skill' affordance that opens the create form", async () => {
    installCrudFetch([BLOG_WRITER]);

    render(<SkillsLibrary />);

    await waitFor(() => {
      expect(screen.getByText(BLOG_WRITER.name)).toBeInTheDocument();
    });
    const newSkill = screen.getByRole("button", { name: /New skill/i });
    expect(newSkill).not.toBeDisabled();

    await userEvent.click(newSkill);
    // The form fields appear.
    expect(screen.getByLabelText(/Name/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Summary/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/System prompt/i)).toBeInTheDocument();
  });

  it("submitting the form POSTs createSkill with the body and refreshes the list", async () => {
    const fetchMock = installCrudFetch([BLOG_WRITER]);

    render(<SkillsLibrary />);
    await waitFor(() => {
      expect(screen.getByText(BLOG_WRITER.name)).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole("button", { name: /New skill/i }));
    await userEvent.type(screen.getByLabelText(/Name/i), "Release Notes");
    await userEvent.type(screen.getByLabelText(/Summary/i), "Summarise merged PRs.");
    await userEvent.type(screen.getByLabelText(/System prompt/i), "Write a release note.");
    await userEvent.click(screen.getByRole("button", { name: /^Create skill$/i }));

    // The POST fired with the create body.
    const postCall = fetchMock.mock.calls.find(
      (c) => ((c[1] as RequestInit | undefined)?.method ?? "GET").toUpperCase() === "POST",
    );
    expect(postCall).toBeDefined();
    const body = JSON.parse((postCall?.[1] as RequestInit).body as string);
    expect(body).toEqual({
      name: "Release Notes",
      summary: "Summarise merged PRs.",
      system_prompt: "Write a release note.",
    });

    // The list refreshed to include the new skill.
    await waitFor(() => {
      expect(screen.getByText("Release Notes")).toBeInTheDocument();
    });
  });

  it("shows a calm inline error on a duplicate (409) and keeps the form usable", async () => {
    installCrudFetch([BLOG_WRITER], 409);

    render(<SkillsLibrary />);
    await waitFor(() => {
      expect(screen.getByText(BLOG_WRITER.name)).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole("button", { name: /New skill/i }));
    await userEvent.type(screen.getByLabelText(/Name/i), "blog-writer");
    await userEvent.type(screen.getByLabelText(/Summary/i), "dup");
    await userEvent.type(screen.getByLabelText(/System prompt/i), "x");
    await userEvent.click(screen.getByRole("button", { name: /^Create skill$/i }));

    await waitFor(() => {
      expect(screen.getByText(/already/i)).toBeInTheDocument();
    });
    // The form stays open + usable (fields still present).
    expect(screen.getByLabelText(/Name/i)).toBeInTheDocument();
  });

  it("validates required fields — does not POST when a field is blank", async () => {
    const fetchMock = installCrudFetch([BLOG_WRITER]);

    render(<SkillsLibrary />);
    await waitFor(() => {
      expect(screen.getByText(BLOG_WRITER.name)).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole("button", { name: /New skill/i }));
    // Submit with everything blank.
    await userEvent.click(screen.getByRole("button", { name: /^Create skill$/i }));

    const postCall = fetchMock.mock.calls.find(
      (c) => ((c[1] as RequestInit | undefined)?.method ?? "GET").toUpperCase() === "POST",
    );
    expect(postCall).toBeUndefined();
  });
});
