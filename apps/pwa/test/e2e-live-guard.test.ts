import {
  E2E_STACK_MARKER,
  assertIsE2EStack,
  assertLoopbackBackend,
  readLiveCredentials,
  resolveBackendUrl,
} from "@/e2e-live/guard";
import { describe, expect, it } from "vitest";

/**
 * The live suite drives a REAL browser through a REAL sign-in with a REAL
 * account. The only thing standing between that and production data is this
 * guard, so the guard gets tested in CI even though the suite it protects
 * cannot run there (it needs docker + GoTrue).
 *
 * These assert PROPOSITIONS, not spellings. A guard written as "reject the
 * hostnames I happen to have thought of" proves only my imagination — the
 * next host I never listed sails through. So each check is an ALLOW-list:
 * everything is refused unless it positively identifies the disposable stack.
 */

describe("assertLoopbackBackend", () => {
  it("accepts a backend on this machine", () => {
    expect(assertLoopbackBackend("http://127.0.0.1:8710")).toBe("http://127.0.0.1:8710");
    expect(assertLoopbackBackend("http://localhost:8710")).toBe("http://localhost:8710");
  });

  it("refuses a remote backend", () => {
    expect(() => assertLoopbackBackend("https://api.bsvibe.dev")).toThrow(/loopback/i);
  });

  it("refuses a host it was never taught about", () => {
    // The point of an allow-list: a host nobody enumerated is still refused.
    expect(() => assertLoopbackBackend("http://10.0.0.7:8710")).toThrow(/loopback/i);
    expect(() => assertLoopbackBackend("http://bsserver:18100")).toThrow(/loopback/i);
  });

  it("refuses a URL that is not parseable as one", () => {
    expect(() => assertLoopbackBackend("not a url")).toThrow();
  });
});

describe("assertIsE2EStack", () => {
  const url = "http://127.0.0.1:8710";

  it("accepts the stack the suite started", () => {
    expect(() =>
      assertIsE2EStack({ status: "ok", version: "0.1.0", git_sha: E2E_STACK_MARKER }, url),
    ).not.toThrow();
  });

  /**
   * ⭐ The proposition this whole guard exists for.
   *
   * On the founder's Mac the PRODUCTION stack is published on loopback too
   * (`docker compose ls` → project `bsvibe-prod`, `0.0.0.0:8700->8000`). So
   * "the host is 127.0.0.1" is NOT evidence that the backend is disposable —
   * a suite that stopped at the loopback check would have signed a real
   * account into production and written real rows. Identifying the stack
   * POSITIVELY, by the marker only this overlay stamps, is what closes it.
   */
  it("refuses production even though production answers on loopback", () => {
    const prodHealth = { status: "ok", version: "0.1.0", git_sha: "b3661b4" };
    expect(() => assertIsE2EStack(prodHealth, url)).toThrow(/e2e-live-stack/);
  });

  it("refuses a backend that reports no marker at all", () => {
    expect(() => assertIsE2EStack({ status: "ok" }, url)).toThrow(/e2e-live-stack/);
    expect(() => assertIsE2EStack(null, url)).toThrow();
  });

  it("names the URL it refused, so the failure is actionable", () => {
    expect(() => assertIsE2EStack({ git_sha: "b3661b4" }, url)).toThrow(/127\.0\.0\.1:8710/);
  });
});

describe("readLiveCredentials", () => {
  it("reads the account from the environment", () => {
    expect(
      readLiveCredentials({
        BSVIBE_E2E_EMAIL: "admin@bsvibe.dev",
        BSVIBE_E2E_PASSWORD: "secret",
      }),
    ).toEqual({ email: "admin@bsvibe.dev", password: "secret" });
  });

  /**
   * No default account, ever. A fallback credential in the repo is a
   * committed secret, and a fallback that is merely *wrong* turns a
   * misconfigured run into a confusing 401 instead of a clear message.
   */
  it("refuses to invent a default account", () => {
    expect(() => readLiveCredentials({})).toThrow(/BSVIBE_E2E_EMAIL/);
    expect(() => readLiveCredentials({ BSVIBE_E2E_EMAIL: "a@b.c" })).toThrow(/BSVIBE_E2E_PASSWORD/);
  });

  it("treats an empty value as absent", () => {
    expect(() => readLiveCredentials({ BSVIBE_E2E_EMAIL: "", BSVIBE_E2E_PASSWORD: "x" })).toThrow(
      /BSVIBE_E2E_EMAIL/,
    );
  });
});

describe("resolveBackendUrl", () => {
  it("defaults to the disposable stack, not the port production occupies", () => {
    // 8700 is the local stack's documented port AND the port `bsvibe-prod`
    // binds on this machine. The default must not be it.
    expect(resolveBackendUrl({})).toBe("http://127.0.0.1:8710");
    expect(resolveBackendUrl({})).not.toContain("8700");
  });

  it("honours an override, but still refuses a remote one", () => {
    expect(resolveBackendUrl({ BSVIBE_E2E_BACKEND_URL: "http://127.0.0.1:9999" })).toBe(
      "http://127.0.0.1:9999",
    );
    expect(() => resolveBackendUrl({ BSVIBE_E2E_BACKEND_URL: "https://api.bsvibe.dev" })).toThrow(
      /loopback/i,
    );
  });
});
