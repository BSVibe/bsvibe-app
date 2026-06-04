"use client";

import { AuthBrand } from "@/components/auth/AuthBrand";
import { ApiError } from "@/lib/api/client";
import {
  type AuthorizeParams,
  type OAuthClientPublic,
  getOAuthClientByClientId,
  postOAuthAuthorize,
} from "@/lib/api/oauth";
import { useHydrated, useSession } from "@/lib/auth/session";
import { useTranslations } from "next-intl";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

/** Allow/Deny screen for browser-initiated OAuth flows (Claude Code,
 *  MCP Inspector, …). Reads the OAuth params from the query string,
 *  fetches the client metadata, and POSTs the decision back to the
 *  backend with the Supabase Bearer attached. The backend response is
 *  `{redirect_to}` — we navigate there to land at the OAuth client's
 *  loopback callback (or back at the redirect URI with
 *  `?error=access_denied` on Deny). */
export function ConsentClient() {
  const router = useRouter();
  const params = useSearchParams();
  const session = useSession();
  const hydrated = useHydrated();
  const t = useTranslations("oauthConsent");

  // The backend redirects here with `?error=invalid_client` etc when
  // the OAuth params didn't validate. Render a clean error instead of
  // trying to fetch the (unknown) client.
  const upstreamError = params.get("error");
  const clientId = params.get("client_id");

  const [client, setClient] = useState<OAuthClientPublic | null>(null);
  const [loadError, setLoadError] = useState<"unknown_client" | "load_failed" | null>(null);
  const [submitting, setSubmitting] = useState<"approve" | "deny" | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // Auth gate: once hydrated, bounce to /login with a return_to that
  // brings the user back to THIS consent URL (preserving every param).
  // Living outside the `(app)` segment means we don't inherit the
  // app-shell RequireAuth — the redirect is owned here.
  useEffect(() => {
    if (!hydrated || session) return;
    const returnTo = `${window.location.pathname}${window.location.search}`;
    router.replace(`/login?return_to=${encodeURIComponent(returnTo)}`);
  }, [hydrated, session, router]);

  // Fetch the client metadata once we have a session + a client_id and
  // no upstream error. The fetch carries the Supabase Bearer (apiFetch
  // attaches it from the session store) — although the endpoint itself
  // is auth-free, sending the header is harmless.
  useEffect(() => {
    if (!hydrated || !session || upstreamError) return;
    if (!clientId) {
      setLoadError("load_failed");
      return;
    }
    let cancelled = false;
    getOAuthClientByClientId(clientId)
      .then((row) => {
        if (!cancelled) setClient(row);
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) {
          setLoadError("unknown_client");
        } else {
          setLoadError("load_failed");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [hydrated, session, clientId, upstreamError]);

  // Render gates: until we know if a session exists (or the redirect
  // to /login fires) keep the surface visually quiet — no flash of
  // "Allow X" before we even know who the user is.
  if (!hydrated || !session) {
    return (
      <main className="login">
        <div className="login__card">
          <AuthBrand />
          <div className="login__status" />
        </div>
      </main>
    );
  }

  if (upstreamError) {
    return (
      <ErrorCard
        title={t("errorTitle")}
        message={t(errorMessageKey(upstreamError))}
        retryHref="/brief"
        retryLabel={t("backHome")}
      />
    );
  }

  if (loadError === "unknown_client") {
    return (
      <ErrorCard
        title={t("errorTitle")}
        message={t("errorUnknownClient")}
        retryHref="/brief"
        retryLabel={t("backHome")}
      />
    );
  }

  if (loadError === "load_failed") {
    return (
      <ErrorCard
        title={t("errorTitle")}
        message={t("errorLoadFailed")}
        retryHref="/brief"
        retryLabel={t("backHome")}
      />
    );
  }

  if (!client) {
    return (
      <main className="login">
        <div className="login__card">
          <AuthBrand />
          <div className="login__status">
            <p className="login__note">{t("loading")}</p>
          </div>
        </div>
      </main>
    );
  }

  const requestedScopes = extractScopeList(params.get("scope"));

  async function handleDecision(action: "approve" | "deny") {
    const authorizeParams = readAuthorizeParams(params);
    if (!authorizeParams) {
      setSubmitError(t("errorLoadFailed"));
      return;
    }
    setSubmitting(action);
    setSubmitError(null);
    try {
      const { redirect_to } = await postOAuthAuthorize(authorizeParams, action);
      // Final hop must be a top-level navigation: the OAuth client's
      // callback is on `http://localhost:NNNN/callback` which the
      // browser fetch can't talk to cross-origin.
      window.location.href = redirect_to;
    } catch {
      setSubmitting(null);
      setSubmitError(t("errorSubmitFailed"));
    }
  }

  const busy = submitting !== null;

  return (
    <main className="login">
      <div className="login__card">
        <AuthBrand />
        <div className="login__head">
          <h1 className="login__title">{t("title", { clientName: client.client_name })}</h1>
          <p className="login__subtitle">{t("subtitle")}</p>
        </div>

        <section className="oauth-consent__scopes" aria-label={t("scopesLabel")}>
          <h2 className="oauth-consent__scopes-title">{t("scopesHeading")}</h2>
          <ul className="oauth-consent__scope-list">
            {requestedScopes.map((scope) => (
              <li key={scope} className="oauth-consent__scope-item">
                <code className="oauth-consent__scope-name">{scope}</code>
                <span className="oauth-consent__scope-desc">{t(scopeDescriptionKey(scope))}</span>
              </li>
            ))}
          </ul>
        </section>

        {submitError && (
          <p className="login__error" role="alert">
            {submitError}
          </p>
        )}

        <div className="oauth-consent__actions">
          <button
            type="button"
            className="login__oauth"
            disabled={busy}
            onClick={() => handleDecision("deny")}
          >
            {submitting === "deny" ? t("denying") : t("deny")}
          </button>
          <button
            type="button"
            className="login__submit"
            disabled={busy}
            onClick={() => handleDecision("approve")}
          >
            {submitting === "approve" ? t("allowing") : t("allow")}
          </button>
        </div>
      </div>
    </main>
  );
}

function ErrorCard({
  title,
  message,
  retryHref,
  retryLabel,
}: {
  title: string;
  message: string;
  retryHref: string;
  retryLabel: string;
}) {
  return (
    <main className="login">
      <div className="login__card">
        <AuthBrand />
        <div className="login__head">
          <h1 className="login__title">{title}</h1>
          <p className="login__subtitle">{message}</p>
        </div>
        <a className="login__back" href={retryHref}>
          {retryLabel}
        </a>
      </div>
    </main>
  );
}

/** OAuth `scope` is space-separated. Empty → empty list. */
function extractScopeList(raw: string | null): string[] {
  if (!raw) return [];
  return raw.split(/\s+/).filter(Boolean);
}

/** Map an OAuth scope to its i18n description key — unknown scopes fall
 *  back to a generic label so the surface stays unbroken when the
 *  scope catalog grows ahead of this file. */
function scopeDescriptionKey(scope: string): string {
  switch (scope) {
    case "mcp:read":
      return "scopeReadDesc";
    case "mcp:write":
      return "scopeWriteDesc";
    case "mcp:admin":
      return "scopeAdminDesc";
    default:
      return "scopeUnknownDesc";
  }
}

/** Read the OAuth params off the URL into the shape `postOAuthAuthorize`
 *  expects. Returns `null` when any required field is missing — the
 *  backend already validated these on the GET, so a miss here is a
 *  malformed URL the user opened directly. */
function readAuthorizeParams(params: URLSearchParams): AuthorizeParams | null {
  const responseType = params.get("response_type");
  const clientId = params.get("client_id");
  const redirectUri = params.get("redirect_uri");
  const codeChallenge = params.get("code_challenge");
  const codeChallengeMethod = params.get("code_challenge_method");
  if (!responseType || !clientId || !redirectUri || !codeChallenge || !codeChallengeMethod) {
    return null;
  }
  const out: AuthorizeParams = {
    response_type: responseType,
    client_id: clientId,
    redirect_uri: redirectUri,
    code_challenge: codeChallenge,
    code_challenge_method: codeChallengeMethod,
  };
  const scope = params.get("scope");
  const state = params.get("state");
  const resource = params.get("resource");
  if (scope) out.scope = scope;
  if (state) out.state = state;
  if (resource) out.resource = resource;
  return out;
}

/** Map an upstream `?error=` value to a friendly i18n key. */
function errorMessageKey(error: string): string {
  switch (error) {
    case "invalid_client":
      return "errorUnknownClient";
    case "invalid_request":
      return "errorInvalidRequest";
    case "unsupported_response_type":
      return "errorInvalidRequest";
    default:
      return "errorLoadFailed";
  }
}
