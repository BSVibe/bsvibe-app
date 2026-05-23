/** Model-accounts API — REAL backend `/api/v1/accounts`
 *  (backend/api/v1/accounts.py): the founder's front door for registering the
 *  per-workspace LLM account the agent loop's model-account resolution needs.
 *  Without an active model account the worker can't run work (it raises a
 *  needs-decision / pauses the run), so this section is load-bearing.
 *
 *   GET    /api/v1/accounts            — list registered model accounts; never
 *                                        the credential, only `has_api_key`
 *   POST   /api/v1/accounts            — register one (201). The plaintext
 *                                        `api_key` is encrypted server-side and
 *                                        NEVER echoed back in the response.
 *   PATCH  /api/v1/accounts/{id}       — partial update: activate / deactivate
 *                                        (`is_active`) or rotate a field.
 *   DELETE /api/v1/accounts/{id}       — hard-delete (revoke), 204 No Content.
 *
 *  The create body mirrors the backend `ModelAccountCreate` (extra=forbid) 1:1:
 *  we only send fields the schema declares, dropping optional ones when blank so
 *  the wire shape stays minimal and the validator never 422s on a stray empty
 *  string. We never read the secret back; the 201 carries `has_api_key: true`. */

import { apiFetch } from "./client";
import type { ModelAccount, ModelAccountCreate, ModelAccountUpdate } from "./types";

/** Registered model accounts for the active workspace. */
export function listAccounts(): Promise<ModelAccount[]> {
  return apiFetch<ModelAccount[]>("/api/v1/accounts");
}

/** Register a model account. The plaintext `api_key` is sent once; the server
 *  encrypts it at rest and the 201 response never echoes it back. We build the
 *  body to match the backend extra=forbid schema: drop `api_base` when blank,
 *  always send `extra_params` (default `{}`). */
export function createAccount(input: ModelAccountCreate): Promise<ModelAccount> {
  const body: ModelAccountCreate = {
    provider: input.provider,
    label: input.label,
    litellm_model: input.litellm_model,
    api_key: input.api_key,
    data_jurisdiction: input.data_jurisdiction,
    extra_params: input.extra_params ?? {},
  };
  const apiBase = input.api_base?.trim();
  if (apiBase) body.api_base = apiBase;

  return apiFetch<ModelAccount>("/api/v1/accounts", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Partially update a model account — used for activate / deactivate by sending
 *  just `{ is_active }`. Returns the refreshed ModelAccount (never the secret).
 *  Only the keys present on `patch` are sent, so a toggle never disturbs other
 *  fields. */
export function updateAccount(id: string, patch: ModelAccountUpdate): Promise<ModelAccount> {
  return apiFetch<ModelAccount>(`/api/v1/accounts/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

/** Convenience: flip a model account's active state via PATCH. */
export function setAccountActive(id: string, isActive: boolean): Promise<ModelAccount> {
  return updateAccount(id, { is_active: isActive });
}

/** Revoke (hard-delete) a model account. 204 No Content, so this resolves to
 *  void. */
export function revokeAccount(id: string): Promise<void> {
  return apiFetch<void>(`/api/v1/accounts/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}
