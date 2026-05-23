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

/** Resolve a paused-run checkpoint with the founder's answer and resume the
 *  run. The backend ResolveRequest is extra=forbid and requires a non-empty
 *  `answer` (min_length=1), so the caller must pass real text. */
export function resolveCheckpoint(
  checkpointId: string,
  answer: string,
): Promise<CheckpointResolveResponse> {
  return apiFetch<CheckpointResolveResponse>(`/api/v1/checkpoints/${checkpointId}/resolve`, {
    method: "POST",
    body: JSON.stringify({ answer }),
  });
}
