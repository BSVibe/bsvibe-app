"use client";

import {
  type OAuthClient,
  createOAuthClient,
  deleteOAuthClient,
  listOAuthClients,
} from "@/lib/api/oauth-clients";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";

/**
 * Settings → Developer. OAuth client management for the embedded
 * authorization server (Lift D1).
 *
 * Founder workflow:
 *  1. Click "Add client" → fill name + redirect URI(s) + scopes.
 *  2. Submit → the new `client_id` is shown, copy-pasteable.
 *  3. Paste `client_id` into the external app (e.g. Claude Code's MCP
 *     config); the OAuth flow then bounces the user through `/api/oauth/
 *     authorize` for the one-click consent.
 *
 * The list is paged-lite — D1 expects a handful of clients per founder
 * (Claude Code + IDE plugins + ad-hoc); we render the full list as
 * one column without pagination. Add it later if it stops fitting.
 */

const DEFAULT_SCOPES = ["mcp:read", "mcp:write"] as const;
const ALL_SCOPES = ["mcp:read", "mcp:write", "mcp:admin"] as const;

export default function DeveloperTab() {
  const t = useTranslations("settings.developer");
  const [clients, setClients] = useState<OAuthClient[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const rows = await listOAuthClients();
      setClients(rows);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="general-tab">
      <p className="general-tab__lede">{t("lede")}</p>

      <section className="account-section" aria-label={t("clients.title")}>
        <header className="developer-tab__header">
          <h2 className="section-label">{t("clients.title")}</h2>
          <button
            type="button"
            className="developer-tab__add"
            onClick={() => setShowForm((v) => !v)}
          >
            {showForm ? t("clients.cancel") : t("clients.add")}
          </button>
        </header>

        {showForm && (
          <CreateClientForm
            onCreated={async () => {
              setShowForm(false);
              await refresh();
            }}
          />
        )}

        {error && <p className="developer-tab__error">{error}</p>}

        {clients === null ? (
          <p className="developer-tab__hint">{t("loading")}</p>
        ) : clients.length === 0 ? (
          <p className="developer-tab__hint">{t("clients.empty")}</p>
        ) : (
          <ul className="developer-tab__list">
            {clients.map((c) => (
              <ClientRow key={c.client_id} client={c} onRevoke={refresh} />
            ))}
          </ul>
        )}
      </section>

      <McpEndpointSection />
    </div>
  );
}

/**
 * MCP server endpoint subsection — Lift D2.
 *
 * Surfaces the embedded MCP server URL + a copy-pasteable Claude Code
 * config snippet so the founder can paste a single block into their
 * client. No token is included; Claude Code discovers the OAuth flow
 * via the WWW-Authenticate header that the /mcp endpoint sends on the
 * first unauthenticated request (RFC 9728 / RFC 6750).
 */
function McpEndpointSection() {
  const t = useTranslations("settings.developer.mcp");
  const [endpoint, setEndpoint] = useState<string>("");
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  useEffect(() => {
    // The MCP endpoint sits at the SAME origin as the backend API — derived
    // from window.location at runtime so dev/preview/prod all surface the
    // right URL without an env var. Production sets api.bsvibe.dev; dev uses
    // localhost:8000; preview maps via Vercel's per-PR domain.
    const origin =
      typeof window !== "undefined" ? window.location.origin : "https://api.bsvibe.dev";
    // The PWA is hosted on a separate domain (app.bsvibe.dev) from the API
    // (api.bsvibe.dev); when the PWA origin differs from the API origin we
    // derive the API host from the PWA host by swapping app→api. Production
    // wires VITE_API_URL but a runtime fallback keeps the snippet correct
    // even when the env var is unset (Vercel preview, local dev, etc.).
    let apiOrigin = origin;
    try {
      const url = new URL(origin);
      if (url.hostname.startsWith("app.")) {
        url.hostname = `api.${url.hostname.slice(4)}`;
        apiOrigin = url.origin;
      }
    } catch {
      // Leave the derived origin as-is if URL parsing failed.
    }
    setEndpoint(`${apiOrigin}/mcp`);
  }, []);

  const configSnippet = endpoint
    ? `{
  "mcpServers": {
    "bsvibe": {
      "url": "${endpoint}"
    }
  }
}`
    : "";

  async function copy(key: string, text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedKey(key);
      window.setTimeout(() => setCopiedKey((v) => (v === key ? null : v)), 1500);
    } catch {
      // Clipboard API failures (no HTTPS in dev, locked-down browser) are
      // soft-failed — the values are still selectable on screen.
    }
  }

  return (
    <section className="account-section" aria-label={t("title")}>
      <header className="developer-tab__header">
        <h2 className="section-label">{t("title")}</h2>
      </header>
      <p className="developer-tab__hint">{t("lede")}</p>

      <div className="developer-tab__row">
        <div className="developer-tab__row-main">
          <span className="developer-tab__row-name">{t("endpointLabel")}</span>
          <code className="developer-tab__row-id">{endpoint || "—"}</code>
        </div>
        <button
          type="button"
          className="developer-tab__add"
          onClick={() => endpoint && copy("endpoint", endpoint)}
          disabled={!endpoint}
        >
          {copiedKey === "endpoint" ? t("copied") : t("copy")}
        </button>
      </div>

      <div className="developer-tab__form">
        <label className="developer-tab__label">
          <span>{t("configLabel")}</span>
          <textarea readOnly rows={6} value={configSnippet} />
          <span className="developer-tab__hint-sm">{t("configHint")}</span>
        </label>
        <button
          type="button"
          className="developer-tab__add"
          onClick={() => configSnippet && copy("config", configSnippet)}
          disabled={!configSnippet}
        >
          {copiedKey === "config" ? t("copied") : t("copy")}
        </button>
      </div>

      <fieldset className="developer-tab__scopes">
        <legend>{t("scopesLabel")}</legend>
        <p className="developer-tab__hint-sm">{t("scopeRead")}</p>
        <p className="developer-tab__hint-sm">{t("scopeWrite")}</p>
        <p className="developer-tab__hint-sm">{t("scopeAdmin")}</p>
      </fieldset>
    </section>
  );
}

interface CreateClientFormProps {
  onCreated: () => void | Promise<void>;
}

function CreateClientForm({ onCreated }: CreateClientFormProps) {
  const t = useTranslations("settings.developer");
  const [name, setName] = useState("");
  const [redirect, setRedirect] = useState("http://127.0.0.1/callback");
  const [scopes, setScopes] = useState<Set<string>>(new Set(DEFAULT_SCOPES));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<OAuthClient | null>(null);

  function toggleScope(s: string) {
    setScopes((prev) => {
      const next = new Set(prev);
      if (next.has(s)) {
        next.delete(s);
      } else {
        next.add(s);
      }
      return next;
    });
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const row = await createOAuthClient({
        client_name: name,
        redirect_uris: redirect
          .split(/\s+/)
          .map((s) => s.trim())
          .filter(Boolean),
        allowed_scopes: Array.from(scopes),
      });
      setCreated(row);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  if (created) {
    return (
      <div className="developer-tab__created">
        <p className="developer-tab__created-title">{t("clients.created")}</p>
        <p className="developer-tab__created-hint">{t("clients.createdHint")}</p>
        <div className="developer-tab__copy">
          <code>{created.client_id}</code>
        </div>
        <button
          type="button"
          className="developer-tab__add"
          onClick={() => {
            setCreated(null);
            void onCreated();
          }}
        >
          {t("clients.done")}
        </button>
      </div>
    );
  }

  return (
    <form className="developer-tab__form" onSubmit={handleSubmit}>
      <label className="developer-tab__label">
        <span>{t("clients.nameLabel")}</span>
        <input
          type="text"
          required
          maxLength={120}
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Claude Code"
        />
      </label>
      <label className="developer-tab__label">
        <span>{t("clients.redirectLabel")}</span>
        <textarea
          required
          rows={2}
          value={redirect}
          onChange={(e) => setRedirect(e.target.value)}
          placeholder="http://127.0.0.1/callback"
        />
        <span className="developer-tab__hint-sm">{t("clients.redirectHint")}</span>
      </label>
      <fieldset className="developer-tab__scopes">
        <legend>{t("clients.scopesLabel")}</legend>
        {ALL_SCOPES.map((s) => (
          <label key={s} className="developer-tab__scope">
            <input type="checkbox" checked={scopes.has(s)} onChange={() => toggleScope(s)} />
            <code>{s}</code>
          </label>
        ))}
      </fieldset>
      {error && <p className="developer-tab__error">{error}</p>}
      <button
        type="submit"
        className="developer-tab__submit"
        disabled={busy || name.trim().length === 0}
      >
        {busy ? t("clients.submitting") : t("clients.submit")}
      </button>
    </form>
  );
}

interface ClientRowProps {
  client: OAuthClient;
  onRevoke: () => void | Promise<void>;
}

function ClientRow({ client, onRevoke }: ClientRowProps) {
  const t = useTranslations("settings.developer");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);

  async function handleRevoke() {
    setBusy(true);
    setError(false);
    try {
      await deleteOAuthClient(client.client_id);
      await onRevoke();
    } catch {
      // Surface the failure — a silently-failed revoke reads as "done" and the
      // founder walks away thinking a live client credential is dead when it
      // isn't.
      setError(true);
    } finally {
      setBusy(false);
    }
  }

  const isRevoked = client.revoked_at !== null;
  return (
    <li className="developer-tab__row" data-revoked={isRevoked || undefined}>
      <div className="developer-tab__row-main">
        <span className="developer-tab__row-name">{client.client_name}</span>
        <code className="developer-tab__row-id">{client.client_id}</code>
      </div>
      <div className="developer-tab__row-meta">
        <span>
          {t("clients.scopes")}: {client.allowed_scopes.join(", ")}
        </span>
        <span>
          {t("clients.redirects")}: {client.redirect_uris.join(", ")}
        </span>
      </div>
      {isRevoked ? (
        <span className="developer-tab__revoked">{t("clients.revoked")}</span>
      ) : (
        <>
          <button
            type="button"
            className="developer-tab__revoke"
            onClick={handleRevoke}
            disabled={busy}
          >
            {busy ? t("clients.revoking") : t("clients.revoke")}
          </button>
          {error && (
            <span className="developer-tab__error" role="alert" aria-live="polite">
              {t("clients.revokeError")}
            </span>
          )}
        </>
      )}
    </li>
  );
}
