/**
 * Skills client — wire contracts against a mocked fetch
 * (lib/api/skills.ts → backend /api/v1/skills, READ-ONLY).
 *
 *  - listSkills:  GET /api/v1/skills
 *  - getSkill:    GET /api/v1/skills/{name}
 *
 * There is NO create/update/delete on this API — the backend skills surface is
 * file-system based (skill MD files live in the per-workspace skills dir), so
 * the client carries reads only.
 */

import { ApiError } from "@/lib/api/client";
import { createSkill, getSkill, listSkills, updateSkill } from "@/lib/api/skills";
import type { Skill, SkillCreate, SkillUpdate } from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

function okFetch(body: unknown, status = 200) {
  return vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
  );
}

describe("skills client", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("listSkills GETs /api/v1/skills and parses the rows", async () => {
    const fetchMock = okFetch([SKILL]);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await listSkills();

    expect(res).toEqual([SKILL]);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/skills");
    expect((init.method ?? "GET").toUpperCase()).toBe("GET");
  });

  it("getSkill GETs /api/v1/skills/{name} (name encoded) and parses the manifest", async () => {
    const fetchMock = okFetch(SKILL);
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await getSkill("blog writer");

    expect(res).toEqual(SKILL);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/skills/blog%20writer");
    expect((init.method ?? "GET").toUpperCase()).toBe("GET");
  });

  it("surfaces an ApiError on a non-ok list read", async () => {
    global.fetch = vi.fn(
      async () => new Response("forbidden", { status: 403 }),
    ) as unknown as typeof fetch;

    await expect(listSkills()).rejects.toBeInstanceOf(ApiError);
  });

  it("surfaces an ApiError on an unknown skill (404)", async () => {
    global.fetch = vi.fn(
      async () => new Response("not found", { status: 404 }),
    ) as unknown as typeof fetch;

    await expect(getSkill("nope")).rejects.toBeInstanceOf(ApiError);
  });

  it("createSkill POSTs /api/v1/skills with the create body and parses the 201", async () => {
    const fetchMock = okFetch(SKILL, 201);
    global.fetch = fetchMock as unknown as typeof fetch;

    const input: SkillCreate = {
      name: "Blog Writer",
      summary: "Drafts a technical blog post in the house voice.",
      system_prompt: "You write calm, precise technical prose.",
    };
    const res = await createSkill(input);

    expect(res).toEqual(SKILL);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/skills");
    expect((init.method ?? "GET").toUpperCase()).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual(input);
  });

  it("surfaces an ApiError (409) when the skill name is already taken", async () => {
    global.fetch = vi.fn(
      async () => new Response("conflict", { status: 409 }),
    ) as unknown as typeof fetch;

    await expect(
      createSkill({ name: "blog-writer", summary: "s", system_prompt: "p" }),
    ).rejects.toBeInstanceOf(ApiError);
  });

  it("updateSkill PATCHes /api/v1/skills/{name} (encoded) with the update body", async () => {
    const fetchMock = okFetch(SKILL, 200);
    global.fetch = fetchMock as unknown as typeof fetch;

    const input: SkillUpdate = {
      summary: "Updated summary.",
      system_prompt: "Updated prompt body.",
    };
    const res = await updateSkill("blog writer", input);

    expect(res).toEqual(SKILL);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/skills/blog%20writer");
    expect((init.method ?? "GET").toUpperCase()).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual(input);
  });

  it("surfaces an ApiError (404) when updating an unknown skill", async () => {
    global.fetch = vi.fn(
      async () => new Response("not found", { status: 404 }),
    ) as unknown as typeof fetch;

    await expect(updateSkill("nope", { summary: "s", system_prompt: "p" })).rejects.toBeInstanceOf(
      ApiError,
    );
  });
});
