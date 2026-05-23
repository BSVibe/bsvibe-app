/**
 * JWT-claims display helper. The Supabase access token carries claims we can
 * surface read-only WITHOUT verifying the signature (the backend verifies on
 * every call — this is display-only). The helper base64url-decodes the JWT
 * payload and extracts the bits the Account tab shows: the sign-in providers,
 * the email, and an optional display name. It must NEVER throw — a malformed
 * or absent token returns calm, empty-but-safe defaults.
 */

import { decodeClaims } from "@/lib/auth/claims";
import { describe, expect, it } from "vitest";

/** Build a JWT-shaped string `header.payload.signature` with a base64url
 *  payload, mirroring how Supabase tokens are encoded. */
function makeToken(payload: Record<string, unknown>): string {
  const b64url = (obj: unknown) =>
    Buffer.from(JSON.stringify(obj))
      .toString("base64")
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
  return `${b64url({ alg: "ES256", typ: "JWT" })}.${b64url(payload)}.sig`;
}

describe("decodeClaims", () => {
  it("extracts providers, email, and name from a well-formed token", () => {
    const token = makeToken({
      email: "alex@bsvibe.dev",
      app_metadata: { providers: ["google", "email"], role: "authenticated" },
      user_metadata: { name: "Alex Chen" },
    });

    const claims = decodeClaims(token);

    expect(claims.email).toBe("alex@bsvibe.dev");
    expect(claims.providers).toEqual(["google", "email"]);
    expect(claims.name).toBe("Alex Chen");
    expect(claims.role).toBe("authenticated");
  });

  it("decodes base64url payloads that use - and _ (non-standard base64)", () => {
    // A payload containing bytes that force `-`/`_` in base64url output.
    const token = makeToken({
      email: "a+b/c?@bsvibe.dev",
      app_metadata: { providers: ["github"] },
    });
    const claims = decodeClaims(token);
    expect(claims.email).toBe("a+b/c?@bsvibe.dev");
    expect(claims.providers).toEqual(["github"]);
  });

  it("returns safe defaults (no throw) for a malformed token", () => {
    const claims = decodeClaims("not-a-jwt");
    expect(claims.email).toBeNull();
    expect(claims.providers).toEqual([]);
    expect(claims.name).toBeNull();
    expect(claims.role).toBeNull();
  });

  it("returns safe defaults for a null/empty token", () => {
    expect(decodeClaims(null).providers).toEqual([]);
    expect(decodeClaims("").providers).toEqual([]);
    expect(decodeClaims(undefined).email).toBeNull();
  });

  it("returns safe defaults when the payload is not valid JSON", () => {
    const bad = `${Buffer.from("{}").toString("base64url")}.@@@notbase64@@@.sig`;
    const claims = decodeClaims(bad);
    expect(claims.providers).toEqual([]);
    expect(claims.email).toBeNull();
  });

  it("tolerates a token whose providers claim is missing or not an array", () => {
    const noProviders = makeToken({ email: "x@bsvibe.dev", app_metadata: {} });
    expect(decodeClaims(noProviders).providers).toEqual([]);

    const badProviders = makeToken({ app_metadata: { providers: "google" } });
    expect(decodeClaims(badProviders).providers).toEqual([]);
  });
});
