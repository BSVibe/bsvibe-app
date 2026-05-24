/** Skills API — REAL backend `/api/v1/skills` (backend/api/v1/skills.py): the
 *  founder's window into the skills loaded for the workspace.
 *
 *   GET   /api/v1/skills          — list skill manifests (name + description +
 *                                   metadata)
 *   GET   /api/v1/skills/{name}   — one skill's manifest by name (404 if absent),
 *                                   incl. the raw `system_prompt` body
 *   POST  /api/v1/skills          — create one; the backend writes a new skill MD
 *                                   file under the per-workspace skills dir and
 *                                   returns the parsed manifest (201). 409 when a
 *                                   skill with that slug already exists.
 *   PATCH /api/v1/skills/{name}   — update the editable body fields (`summary` +
 *                                   `system_prompt`); the slug / name is immutable
 *                                   (404 if absent, 422 on a blank summary).
 *
 *  Mirrors the backend `SkillResponse` / `SkillCreate` / `SkillUpdate` shapes
 *  1:1. DELETE is deferred to a later lift (the backend has no delete path).
 *  Style mirrors lib/api/connectors.ts. */

import { apiFetch } from "./client";
import type { Skill, SkillCreate, SkillUpdate } from "./types";

/** Skills loaded for the active workspace. */
export function listSkills(): Promise<Skill[]> {
  return apiFetch<Skill[]>("/api/v1/skills");
}

/** One skill's manifest by name. Throws `ApiError` (404) when the skill is not
 *  loaded for the workspace. */
export function getSkill(name: string): Promise<Skill> {
  return apiFetch<Skill>(`/api/v1/skills/${encodeURIComponent(name)}`);
}

/** Create a skill. The backend slugifies `name` for the filename + manifest
 *  name, writes the MD file, and returns the parsed manifest (201). Throws
 *  `ApiError` (409) when a skill with that slug already exists, or (422) when the
 *  name can't yield a safe slug. The body mirrors the backend `SkillCreate`
 *  (extra=forbid) 1:1 — exactly `name` / `summary` / `system_prompt`. */
export function createSkill(input: SkillCreate): Promise<Skill> {
  const body: SkillCreate = {
    name: input.name,
    summary: input.summary,
    system_prompt: input.system_prompt,
  };
  return apiFetch<Skill>("/api/v1/skills", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Update a skill's editable body fields. The backend rewrites the MD file's
 *  manifest `description` (from `summary`) + Markdown body (from `system_prompt`),
 *  preserving the immutable `name` / slug, and returns the re-parsed manifest
 *  (200). Throws `ApiError` (404) when the skill isn't loaded, or (422) when
 *  `summary` is blank. The body mirrors the backend `SkillUpdate` (extra=forbid)
 *  1:1 — exactly `summary` / `system_prompt` (NO `name`: it is immutable). */
export function updateSkill(name: string, input: SkillUpdate): Promise<Skill> {
  const body: SkillUpdate = {
    summary: input.summary,
    system_prompt: input.system_prompt,
  };
  return apiFetch<Skill>(`/api/v1/skills/${encodeURIComponent(name)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}
