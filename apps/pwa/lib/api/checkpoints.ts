/** Checkpoints API — REAL backend (backend/api/v1/checkpoints.py):
 *   GET  /api/v1/checkpoints              — list PENDING paused-run Decisions
 *   POST /api/v1/checkpoints/{id}/resolve — record the founder's answer + resume
 *
 *  A paused-run checkpoint is a blocking question minted when an agent loop is
 *  stuck (or the work LLM calls `ask_user_question`); the run stays paused until
 *  the founder answers. Resolving records the answer and flips the run RUNNING →
 *  OPEN so the worker re-picks it with the answer in context. */

import { apiFetch } from "./client";
import type { Checkpoint, CheckpointResolveResponse } from "./types";

/** Pending paused-run checkpoints awaiting a founder answer (newest first). */
export function listCheckpoints(): Promise<Checkpoint[]> {
  return apiFetch<Checkpoint[]>("/api/v1/checkpoints");
}

/** Resolve a paused-run checkpoint with the founder's free-text answer and
 *  resume the run. Used for ask_user_question Decisions and the "Other"
 *  free-text fallback (L-D1). The backend rejects empty answers. */
export function resolveCheckpoint(
  checkpointId: string,
  answer: string,
): Promise<CheckpointResolveResponse> {
  return apiFetch<CheckpointResolveResponse>(`/api/v1/checkpoints/${checkpointId}/resolve`, {
    method: "POST",
    body: JSON.stringify({ answer }),
  });
}

/** L-D2 — resolve a paused-run checkpoint via a one-click action
 *  (`retry` / `ship` / `discard`) on an executor B2b Decision. The backend
 *  dispatches to the side-effecting handler; the response carries the new
 *  `run_status` (shipped / cancelled) so the row UI can reflect terminal state.
 *
 *  §14 — `guidance` is the founder's own words typed alongside the button, sent
 *  as `reason`. It is what makes "Guide & retry" mean what it says: the backend
 *  seeds it as the resumption message the re-driven agent reads (and, on a
 *  `discard`, keeps it as negative knowledge). Omitted entirely when blank, so a
 *  text-free action stays text-free on the wire — that is the shape #823
 *  suppresses from knowledge, and it must stay reachable. */
export function resolveCheckpointAction(
  checkpointId: string,
  actionKey: string,
  guidance?: string,
): Promise<CheckpointResolveResponse> {
  const written = (guidance ?? "").trim();
  const body: { action_key: string; reason?: string } = { action_key: actionKey };
  if (written) body.reason = written;
  return apiFetch<CheckpointResolveResponse>(`/api/v1/checkpoints/${checkpointId}/resolve`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}
