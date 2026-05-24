/**
 * Skill editor surface — the edit form for one skill, prefilled from the loaded
 * manifest. Asserts:
 *  - renders the skill `name` read-only (not an editable input) plus editable
 *    `summary` + `system_prompt` fields prefilled from the skill
 *  - Save PATCHes /api/v1/skills/{name} with { summary, system_prompt } and
 *    calls back with the updated skill
 *  - a blank summary or prompt does NOT fire the PATCH (local validation)
 *  - a failed save shows a calm inline error and keeps the form usable
 *  - Cancel calls back without firing a request
 */

import SkillEditor from "@/components/skills/SkillEditor";
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

/** Routes PATCH /api/v1/skills/{name}; echoes the body into an updated row. */
function installPatchFetch(status = 200) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (!url.startsWith("/api/v1/skills/")) throw new Error(`unexpected fetch ${url}`);
    if ((init?.method ?? "GET").toUpperCase() !== "PATCH") throw new Error("expected PATCH");
    if (status !== 200) return json("boom", status);
    const body = JSON.parse(init?.body as string) as {
      summary: string;
      system_prompt: string;
    };
    return json({ ...SKILL, description: body.summary, system_prompt: body.system_prompt }, 200);
  });
  global.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

describe("Skill editor surface", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the name read-only plus prefilled summary + system prompt", () => {
    installPatchFetch();
    render(<SkillEditor skill={SKILL} onSaved={vi.fn()} onCancel={vi.fn()} />);

    // Name is shown but is NOT an editable input.
    expect(screen.getByText(SKILL.name)).toBeInTheDocument();
    expect(screen.queryByDisplayValue(SKILL.name)).not.toBeInTheDocument();

    // Summary + prompt are prefilled, editable fields.
    expect(screen.getByLabelText(/Summary/i)).toHaveValue(SKILL.description);
    expect(screen.getByLabelText(/System prompt/i)).toHaveValue(SKILL.system_prompt);
  });

  it("Save PATCHes with the edited body and calls onSaved with the updated skill", async () => {
    const fetchMock = installPatchFetch();
    const onSaved = vi.fn();
    render(<SkillEditor skill={SKILL} onSaved={onSaved} onCancel={vi.fn()} />);

    const summary = screen.getByLabelText(/Summary/i);
    await userEvent.clear(summary);
    await userEvent.type(summary, "A sharper summary.");
    await userEvent.click(screen.getByRole("button", { name: /^Save$/i }));

    const patchCall = fetchMock.mock.calls.find(
      (c) => ((c[1] as RequestInit | undefined)?.method ?? "GET").toUpperCase() === "PATCH",
    );
    expect(patchCall).toBeDefined();
    expect(String(patchCall?.[0])).toBe("/api/v1/skills/blog-writer");
    const body = JSON.parse((patchCall?.[1] as RequestInit).body as string);
    expect(body).toEqual({
      summary: "A sharper summary.",
      system_prompt: SKILL.system_prompt,
    });

    await waitFor(() => {
      expect(onSaved).toHaveBeenCalledWith(
        expect.objectContaining({ description: "A sharper summary." }),
      );
    });
  });

  it("does not PATCH when a required field is blank", async () => {
    const fetchMock = installPatchFetch();
    render(<SkillEditor skill={SKILL} onSaved={vi.fn()} onCancel={vi.fn()} />);

    await userEvent.clear(screen.getByLabelText(/Summary/i));
    await userEvent.click(screen.getByRole("button", { name: /^Save$/i }));

    const patchCall = fetchMock.mock.calls.find(
      (c) => ((c[1] as RequestInit | undefined)?.method ?? "GET").toUpperCase() === "PATCH",
    );
    expect(patchCall).toBeUndefined();
  });

  it("shows a calm inline error on a failed save and keeps the form usable", async () => {
    installPatchFetch(500);
    render(<SkillEditor skill={SKILL} onSaved={vi.fn()} onCancel={vi.fn()} />);

    await userEvent.click(screen.getByRole("button", { name: /^Save$/i }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
    });
    // Form stays usable.
    expect(screen.getByLabelText(/Summary/i)).toBeInTheDocument();
  });

  it("Cancel calls onCancel without firing a request", async () => {
    const fetchMock = installPatchFetch();
    const onCancel = vi.fn();
    render(<SkillEditor skill={SKILL} onSaved={vi.fn()} onCancel={onCancel} />);

    await userEvent.click(screen.getByRole("button", { name: /Cancel/i }));

    expect(onCancel).toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
