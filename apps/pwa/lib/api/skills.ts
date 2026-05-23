/** Skills API — REAL backend `/api/v1/skills` (backend/api/v1/skills.py): the
 *  founder's read-only window into the skills loaded for the workspace.
 *
 *   GET /api/v1/skills          — list skill manifests (name + description +
 *                                 metadata), newest first
 *   GET /api/v1/skills/{name}   — one skill's manifest by name (404 if absent)
 *
 *  Both are read-only. There is NO create/update/delete on the HTTP API — skill
 *  authoring is file-system based (skill MD files live in the per-workspace
 *  skills dir), so this client carries reads only. Mirrors the backend
 *  `SkillResponse` shape 1:1; the markdown system-prompt body is never sent over
 *  the wire (only the `has_system_prompt` flag), so the viewer renders metadata,
 *  not the raw prompt. Style mirrors lib/api/connectors.ts. */

import { apiFetch } from "./client";
import type { Skill } from "./types";

/** Skills loaded for the active workspace. */
export function listSkills(): Promise<Skill[]> {
  return apiFetch<Skill[]>("/api/v1/skills");
}

/** One skill's manifest by name. Throws `ApiError` (404) when the skill is not
 *  loaded for the workspace. */
export function getSkill(name: string): Promise<Skill> {
  return apiFetch<Skill>(`/api/v1/skills/${encodeURIComponent(name)}`);
}
