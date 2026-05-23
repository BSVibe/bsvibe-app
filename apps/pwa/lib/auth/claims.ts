/**
 * JWT-claims display helper — pure, no I/O.
 *
 * The Supabase access token is a signed ES256 JWT. The backend verifies its
 * signature on every request; the client never needs to. For DISPLAY purposes
 * only (showing the signed-in email, which providers are linked, the role) we
 * base64url-decode the payload segment WITHOUT verifying. Trusting these values
 * for an authorization decision would be wrong — but we only render them.
 *
 * The function MUST NOT throw: a malformed token, a non-JSON payload, or an
 * absent token all return calm, empty-but-safe defaults so the Account tab can
 * render unconditionally.
 */

export interface DisplayClaims {
  /** Verified-by-server email, surfaced read-only. */
  email: string | null;
  /** Linked sign-in providers from `app_metadata.providers`, e.g. ["google"]. */
  providers: string[];
  /** Optional display name from `user_metadata.name`. */
  name: string | null;
  /** Supabase role from `app_metadata.role`, e.g. "authenticated". */
  role: string | null;
}

const EMPTY: DisplayClaims = { email: null, providers: [], name: null, role: null };

/** Decode a base64url string to UTF-8, or `null` if it isn't valid. */
function decodeBase64Url(segment: string): string | null {
  try {
    let base64 = segment.replace(/-/g, "+").replace(/_/g, "/");
    // Restore padding that base64url strips.
    const pad = base64.length % 4;
    if (pad === 2) base64 += "==";
    else if (pad === 3) base64 += "=";
    else if (pad === 1) return null; // never a valid base64 length

    if (typeof atob === "function") {
      const binary = atob(base64);
      const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
      return new TextDecoder().decode(bytes);
    }
    // Node fallback (vitest / SSR).
    return Buffer.from(base64, "base64").toString("utf-8");
  } catch {
    return null;
  }
}

/**
 * Extract the display-only claims from a Supabase access token. Never throws.
 */
export function decodeClaims(token: string | null | undefined): DisplayClaims {
  if (typeof token !== "string" || token.length === 0) return { ...EMPTY };

  const parts = token.split(".");
  if (parts.length < 2) return { ...EMPTY };

  const json = decodeBase64Url(parts[1]);
  if (json === null) return { ...EMPTY };

  let payload: Record<string, unknown>;
  try {
    const parsed = JSON.parse(json);
    if (typeof parsed !== "object" || parsed === null) return { ...EMPTY };
    payload = parsed as Record<string, unknown>;
  } catch {
    return { ...EMPTY };
  }

  const appMeta =
    typeof payload.app_metadata === "object" && payload.app_metadata !== null
      ? (payload.app_metadata as Record<string, unknown>)
      : {};
  const userMeta =
    typeof payload.user_metadata === "object" && payload.user_metadata !== null
      ? (payload.user_metadata as Record<string, unknown>)
      : {};

  const rawProviders = appMeta.providers;
  const providers = Array.isArray(rawProviders)
    ? rawProviders.filter((p): p is string => typeof p === "string")
    : [];

  return {
    email: typeof payload.email === "string" ? payload.email : null,
    providers,
    name: typeof userMeta.name === "string" ? userMeta.name : null,
    role: typeof appMeta.role === "string" ? appMeta.role : null,
  };
}
