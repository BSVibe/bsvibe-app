import {
  START_STACK_HINT,
  assertIsE2EStack,
  readLiveCredentials,
  resolveBackendUrl,
} from "./guard";

/**
 * Preflight, run once before any browser starts.
 *
 * Everything that can be known before the suite writes anything is checked
 * here, so a misconfigured run fails in a second with a sentence that says
 * what to fix — instead of failing five minutes later as a puzzling 401, or
 * (the case this exists for) *succeeding* against the wrong backend.
 */
export default async function globalSetup(): Promise<void> {
  const backendUrl = resolveBackendUrl(process.env);

  // Before the credentials are ever put on the wire.
  readLiveCredentials(process.env);

  let health: unknown;
  try {
    const res = await fetch(`${backendUrl}/api/health`, {
      signal: AbortSignal.timeout(5_000),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    health = await res.json();
  } catch (cause) {
    throw new Error(
      `the live E2E backend at ${backendUrl} did not answer /api/health (${String(cause)}). Export BSVIBE_E2E_KMS_KEY_B64=$(openssl rand -base64 32) first, then: ${START_STACK_HINT}`,
      { cause },
    );
  }

  // ⚠️ The one that matters — see `assertIsE2EStack`. Production answers on
  // loopback on this machine too, so reaching *a* backend proves nothing.
  assertIsE2EStack(health, backendUrl);
}
