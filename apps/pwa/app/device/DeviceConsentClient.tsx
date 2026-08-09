"use client";

import { AuthBrand } from "@/components/auth/AuthBrand";
import { RETURN_TO_KEY } from "@/lib/api/auth";
import { ApiError } from "@/lib/api/client";
import { type DeviceRequest, decideDeviceRequest, getDeviceRequest } from "@/lib/api/oauth";
import { useHydrated, useSession } from "@/lib/auth/session";
import { useTranslations } from "next-intl";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

/**
 * RFC 8628 consent — the browser half of a device sign-in.
 *
 * The device that started this has no browser and cannot receive a redirect;
 * it is polling `/token` right now. So unlike `/oauth/consent`, this screen
 * hands NOTHING back — no code, no redirect, no token. It records a decision
 * and tells the human they can walk away. That asymmetry is the whole reason
 * the flow is safe to drive from a chat window or a phone.
 */
export function DeviceConsentClient() {
  const router = useRouter();
  const params = useSearchParams();
  const session = useSession();
  const hydrated = useHydrated();
  const t = useTranslations("deviceConsent");

  // `verification_uri_complete` puts the code in the URL; `verification_uri`
  // does not, and then the human types it.
  const codeFromUrl = params.get("user_code") ?? "";
  const [typed, setTyped] = useState(codeFromUrl);
  const [code, setCode] = useState(codeFromUrl);
  const [request, setRequest] = useState<DeviceRequest | null>(null);
  const [loadError, setLoadError] = useState<"unknown" | "failed" | null>(null);
  const [submitting, setSubmitting] = useState<"approve" | "deny" | null>(null);
  const [decided, setDecided] = useState<"approved" | "denied" | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // Auth gate — same shape as /oauth/consent: this route lives outside the
  // `(app)` segment, so it owns its own redirect back to itself.
  useEffect(() => {
    if (!hydrated || session) return;
    const returnTo = `${window.location.pathname}${window.location.search}`;
    sessionStorage.setItem(RETURN_TO_KEY, returnTo);
    router.replace(`/login?return_to=${encodeURIComponent(returnTo)}`);
  }, [hydrated, session, router]);

  useEffect(() => {
    if (!hydrated || !session || !code) return;
    let cancelled = false;
    setLoadError(null);
    getDeviceRequest(code)
      .then((row) => {
        if (!cancelled) setRequest(row);
      })
      .catch((err) => {
        if (cancelled) return;
        setRequest(null);
        setLoadError(err instanceof ApiError && err.status === 404 ? "unknown" : "failed");
      });
    return () => {
      cancelled = true;
    };
  }, [hydrated, session, code]);

  const decide = useCallback(
    async (approve: boolean) => {
      setSubmitting(approve ? "approve" : "deny");
      setSubmitError(null);
      try {
        const result = await decideDeviceRequest(code, approve);
        setDecided(result.status === "denied" ? "denied" : "approved");
      } catch {
        setSubmitError(t("errorSubmit"));
      } finally {
        setSubmitting(null);
      }
    },
    [code, t],
  );

  if (!hydrated || !session) {
    return <Shell>{null}</Shell>;
  }

  if (decided) {
    return (
      <Shell title={t(decided === "approved" ? "approvedTitle" : "deniedTitle")}>
        <p className="login__subtitle">
          {t(decided === "approved" ? "approvedBody" : "deniedBody")}
        </p>
      </Shell>
    );
  }

  // No code yet — ask for it.
  if (!code) {
    return (
      <Shell title={t("title")}>
        <p className="login__subtitle">{t("enterHint")}</p>
        <form
          className="device-consent__form"
          onSubmit={(e) => {
            e.preventDefault();
            setCode(typed.trim());
          }}
        >
          <label className="developer-tab__label">
            <span>{t("codeLabel")}</span>
            <input
              type="text"
              autoComplete="off"
              spellCheck={false}
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              placeholder="WXYZ-2345"
            />
          </label>
          <button type="submit" className="login__submit" disabled={!typed.trim()}>
            {t("continue")}
          </button>
        </form>
      </Shell>
    );
  }

  if (loadError) {
    return (
      <Shell title={t("errorTitle")}>
        <p className="login__subtitle">
          {t(loadError === "unknown" ? "errorUnknownCode" : "errorLoadFailed")}
        </p>
        <button type="button" className="login__oauth" onClick={() => setCode("")}>
          {t("tryAnotherCode")}
        </button>
      </Shell>
    );
  }

  if (!request) {
    return <Shell title={t("title")}>{<p className="login__note">{t("loading")}</p>}</Shell>;
  }

  // Already decided or dead — say so rather than offering a button that fails.
  if (request.status !== "pending") {
    return (
      <Shell title={t("title")}>
        <p className="login__subtitle">{t(`status_${request.status}`)}</p>
        <button type="button" className="login__oauth" onClick={() => setCode("")}>
          {t("tryAnotherCode")}
        </button>
      </Shell>
    );
  }

  const busy = submitting !== null;
  return (
    <Shell title={t("confirmTitle", { clientId: request.client_id })}>
      <p className="login__subtitle">{t("confirmBody")}</p>

      <section className="oauth-consent__scopes" aria-label={t("scopesLabel")}>
        <h2 className="oauth-consent__scopes-title">{t("scopesHeading")}</h2>
        <ul className="oauth-consent__scope-list">
          {request.scope.map((scope) => (
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
          onClick={() => decide(false)}
        >
          {submitting === "deny" ? t("denying") : t("deny")}
        </button>
        <button
          type="button"
          className="login__submit"
          disabled={busy}
          onClick={() => decide(true)}
        >
          {submitting === "approve" ? t("allowing") : t("allow")}
        </button>
      </div>
    </Shell>
  );
}

function Shell({ title, children }: { title?: string; children: React.ReactNode }) {
  return (
    <main className="login">
      <div className="login__card">
        <AuthBrand />
        {title && (
          <div className="login__head">
            <h1 className="login__title">{title}</h1>
          </div>
        )}
        {children}
      </div>
    </main>
  );
}

/** Unknown scopes fall back to a generic label so a growing catalog never
 *  renders a blank row. Mirrors the /oauth/consent mapping. */
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
