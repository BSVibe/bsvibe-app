/** Skills API — REAL backend `/api/v1/skills` (backend/api/v1/skills.py): the
 *  founder's window into the skills loaded for the workspace.
 *
 *   GET  /api/v1/skills          — list skill manifests (name + description +
 *                                  metadata)
 *   GET  /api/v1/skills/{name}   — one skill's manifest by name (404 if absent)
 *   POST /api/v1/skills          — create one; the backend writes a new skill MD
 *                                  file under the per-workspace skills dir and
 *                                  returns the parsed manifest (201). 409 when a
 *                                  skill with that slug already exists.
 *
 *  CREATE only — there is NO update/delete on the HTTP API yet (deferred to a
 *  later lift). Mirrors the backend `SkillResponse` / `SkillCreate` shapes 1:1;
 *  the markdown system-prompt body is never returned over the wire (only the
 *  `has_system_prompt` flag), so the viewer renders metadata, not the raw prompt.
 *  Style mirrors lib/api/connectors.ts. */

import { apiFetch } from "./client";
import type { Skill, SkillCreate } from "./types";

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
