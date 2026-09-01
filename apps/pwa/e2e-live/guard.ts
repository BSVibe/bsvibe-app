/**
 * Safety rails for the **authenticated** browser E2E suite.
 *
 * This suite is different in kind from `e2e/`. That one points the PWA at an
 * unreachable address and can only judge the pre-auth surface. This one signs
 * a REAL account into a REAL GoTrue and drives the app behind the gate, so a
 * misconfigured run does not fail — it succeeds against the wrong backend and
 * writes rows there. These functions are what stands in the way.
 *
 * They are exercised by `test/e2e-live-guard.test.ts`, which CI runs, even
 * though the suite they protect cannot run in CI (it needs docker + GoTrue).
 * A guard nothing tests is a guard nobody knows is dead.
 */

/**
 * The `git_sha` that `deploy/compose.e2e-live.yaml` stamps onto its backend.
 * Keep the two in sync — the overlay's comment says so on its side too.
 */
export const E2E_STACK_MARKER = "e2e-live-stack";

/** How to bring the disposable stack up — appended to the refusals below. */
export const START_STACK_HINT =
  "Start it with:\n  docker compose -f deploy/compose.yaml -f deploy/compose.e2e-live.yaml up -d postgres redis backend";

/** Why a non-disposable backend is refused rather than merely warned about. */
const REAL_SIGNIN_WARNING =
  "This suite performs a real sign-in and writes real rows; it may only ever talk to the disposable stack in deploy/compose.e2e-live.yaml.";

/**
 * Hosts that are unambiguously *this machine*. An allow-list, not a
 * deny-list: a host nobody thought to enumerate is refused rather than
 * admitted, so the guard's coverage does not depend on my imagination.
 */
const LOOPBACK_HOSTS: ReadonlySet<string> = new Set(["127.0.0.1", "localhost", "::1", "[::1]"]);

/** Refuse any backend that is not on this machine. Returns the URL unchanged. */
export function assertLoopbackBackend(rawUrl: string): string {
  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    throw new Error(
      `BSVIBE_E2E_BACKEND_URL is not a URL: ${JSON.stringify(rawUrl)}. Expected something like http://127.0.0.1:8710.`,
    );
  }
  if (!LOOPBACK_HOSTS.has(parsed.hostname)) {
    throw new Error(
      `refusing to run the live E2E suite against ${rawUrl} — the backend must be on loopback (this machine). Host ${JSON.stringify(parsed.hostname)} is not. ${REAL_SIGNIN_WARNING}`,
    );
  }
  return rawUrl;
}

/**
 * Refuse any backend that is not *the disposable stack this suite started*.
 *
 * ⚠️ This is the check that actually matters, and it is easy to think the
 * loopback check above already covers it. It does not. On the founder's Mac
 * the PRODUCTION stack is published on loopback as well:
 *
 *     docker compose ls
 *     bsvibe-prod   running(6)   …   0.0.0.0:8700->8000/tcp
 *
 * So "127.0.0.1" is a statement about the network, not about which database
 * is behind it — and 8700 vs 8710 is one keystroke. Rather than enumerate the
 * ports or shas that mean "prod" (a list that rots the moment prod deploys),
 * this identifies the intended stack POSITIVELY: only the overlay stamps
 * `git_sha=e2e-live-stack`, so anything else — prod, the local dev stack, a
 * stale container — fails closed.
 */
export function assertIsE2EStack(health: unknown, backendUrl: string): void {
  const sha = (health as { git_sha?: unknown } | null | undefined)?.git_sha;
  if (sha === E2E_STACK_MARKER) return;
  throw new Error(
    `refusing to run the live E2E suite against ${backendUrl} — /api/health reports git_sha=${JSON.stringify(sha)}, not ${JSON.stringify(E2E_STACK_MARKER)}. That is NOT the disposable stack, and this suite signs in for real. ${START_STACK_HINT}`,
  );
}

/** The account the suite signs in as. */
export interface LiveCredentials {
  readonly email: string;
  readonly password: string;
}

/**
 * Read the test account from the environment — with no default, ever.
 *
 * A committed fallback account is a committed secret. A fallback that is
 * merely stale is worse than nothing: the run gets a 401 from GoTrue and the
 * failure reads as "the login screen is broken" instead of "you did not set
 * the variable".
 */
export function readLiveCredentials(env: Record<string, string | undefined>): LiveCredentials {
  const email = env.BSVIBE_E2E_EMAIL?.trim();
  const password = env.BSVIBE_E2E_PASSWORD;
  if (!email) {
    throw new Error(
      "BSVIBE_E2E_EMAIL is not set. The live suite signs in as a real SSO account; " +
        "it will not guess one. Export the test account before running.",
    );
  }
  if (!password) {
    throw new Error(
      "BSVIBE_E2E_PASSWORD is not set (BSVIBE_E2E_EMAIL was). Export the test " +
        "account's password before running.",
    );
  }
  return { email, password };
}

/**
 * The disposable stack's backend, as published by `compose.e2e-live.yaml`.
 * NOT 8700 — that is the port `compose.yaml` documents for the local stack
 * and the port the **production** stack actually occupies on this machine.
 */
export const DEFAULT_E2E_BACKEND_URL = "http://127.0.0.1:8710";

/** Resolve the backend URL from the environment and refuse a remote one. */
export function resolveBackendUrl(env: Record<string, string | undefined>): string {
  return assertLoopbackBackend(env.BSVIBE_E2E_BACKEND_URL?.trim() || DEFAULT_E2E_BACKEND_URL);
}
