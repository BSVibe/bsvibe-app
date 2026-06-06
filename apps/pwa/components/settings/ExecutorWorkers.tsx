"use client";

import { backendBaseUrl } from "@/lib/api/client";
import type { Worker } from "@/lib/api/types";
import { listWorkers, mintInstallToken, revokeWorker } from "@/lib/api/workers";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import CopyField from "./CopyField";

/**
 * Settings → Models → "Executor workers" — GitHub-Actions-runner-style UX
 * (Lift E4).
 *
 * The founder registers a host that already has their coding-agent CLIs
 * (claude_code / codex / opencode) signed in. The new flow is a single
 * shell snippet they paste once on the host:
 *
 *   pip install bsvibe-app           # or any future packaging
 *   bsvibe login                     # PKCE loopback OAuth on the host
 *   bsvibe-worker register --name $(hostname)
 *   bsvibe-worker run
 *
 * No install-token paste. The CLI uses the host's OAuth credentials
 * (`~/.config/bsvibe/credentials.json`) to authenticate against
 * `POST /api/v1/workers/register`, the backend derives the workspace from
 * the verified bearer, and the per-worker token comes back in the response
 * so the daemon can heartbeat / poll / report.
 *
 * Backed by the REAL /api/v1/workers endpoints (backend/api/v1/workers.py):
 *
 *   - List   ← GET    /api/v1/workers
 *   - Revoke → DELETE /api/v1/workers/{id}
 *   - (Deprecated) Mint legacy install token — `mintInstallToken()` is kept
 *     for hosts that haven't cut over yet; surfaced behind "Show legacy
 *     install token" until Lift E5 removes the endpoint entirely.
 */
type ListState = { data: Worker[]; failed: boolean } | null;

export default function ExecutorWorkers() {
  const [list, setList] = useState<ListState>(null);
  const [showInstall, setShowInstall] = useState(false);
  const [showLegacy, setShowLegacy] = useState(false);
  const t = useTranslations("settings.models.workers");

  async function load() {
    try {
      setList({ data: await listWorkers(), failed: false });
    } catch {
      setList({ data: [], failed: true });
    }
  }

  useEffect(() => {
    let active = true;
    listWorkers()
      .then((data) => active && setList({ data, failed: false }))
      .catch(() => active && setList({ data: [], failed: true }));
    return () => {
      active = false;
    };
  }, []);

  const workers = list && !list.failed ? list.data : [];

  return (
    <section className="workers" aria-label={t("sectionLabel")}>
      <header className="workers__head">
        <h2 className="section-label">{t("sectionHeading")}</h2>
        {list && !list.failed && list.data.length > 0 ? (
          <span className="workers__count">{list.data.length}</span>
        ) : null}
      </header>
      <p className="workers__lede">{t("lede")}</p>

      {/* PRIMARY — runner-style install command. No install-token paste. */}
      {showInstall ? (
        <section className="worker-install" aria-label={t("installLabel")}>
          <p className="worker-install__title">{t("installTitle")}</p>
          <p className="worker-install__hint">{t("installHint")}</p>
          <CopyField
            label={t("installCommandLabel")}
            value={t("installCommand", { serverUrl: backendBaseUrl() })}
          />
          <p className="worker-install__notes">{t("installNotes")}</p>
          <button
            type="button"
            className="worker-install__done"
            onClick={() => setShowInstall(false)}
          >
            {t("done")}
          </button>
          <LegacyInstallTokenToggle
            visible={showLegacy}
            onToggle={() => setShowLegacy((v) => !v)}
          />
          {showLegacy ? <LegacyInstallTokenPanel /> : null}
        </section>
      ) : (
        <div className="workers__connect">
          <button
            type="button"
            className="workers__connect-btn"
            onClick={() => setShowInstall(true)}
          >
            {t("addWorker")}
          </button>
        </div>
      )}

      {list === null ? (
        <p className="workers__loading" aria-busy="true">
          {t("loading")}
        </p>
      ) : list.failed ? (
        <p className="workers__note" aria-live="polite">
          {t("loadError")}
        </p>
      ) : workers.length === 0 ? (
        <p className="workers__empty">{t("empty")}</p>
      ) : (
        <ul className="workers__list" aria-label={t("listLabel")}>
          {workers.map((w) => (
            <WorkerRow key={w.id} worker={w} onRevoked={load} revoke={revokeWorker} />
          ))}
        </ul>
      )}
    </section>
  );
}

function LegacyInstallTokenToggle({
  visible,
  onToggle,
}: {
  visible: boolean;
  onToggle: () => void;
}) {
  const t = useTranslations("settings.models.workers");
  return (
    <button
      type="button"
      className="worker-install__legacy-toggle"
      onClick={onToggle}
      aria-expanded={visible}
    >
      {visible ? t("legacyHide") : t("legacyShow")}
    </button>
  );
}

type LegacyMintState = "idle" | "minting" | "error";

function LegacyInstallTokenPanel() {
  const t = useTranslations("settings.models.workers");
  const [state, setState] = useState<LegacyMintState>("idle");
  const [token, setToken] = useState<string | null>(null);

  async function mint() {
    if (state === "minting") return;
    setState("minting");
    try {
      const minted = await mintInstallToken();
      setToken(minted.token);
      setState("idle");
    } catch {
      setState("error");
    }
  }

  if (token) {
    return (
      <section className="worker-token worker-token--legacy" aria-label={t("legacyTokenLabel")}>
        <p className="worker-token__warn">{t("legacyWarn")}</p>
        <CopyField label={t("tokenLabel")} value={token} secret />
        <CopyField
          label={t("legacyRunLabel")}
          value={t("legacyRunCommand", { token, serverUrl: backendBaseUrl() })}
          secret
        />
        <button type="button" className="worker-token__done" onClick={() => setToken(null)}>
          {t("done")}
        </button>
      </section>
    );
  }
  return (
    <div className="worker-install__legacy">
      <p className="worker-install__legacy-note">{t("legacyNote")}</p>
      {state === "error" ? (
        <span className="workers__error" aria-live="polite">
          {t("mintError")}
        </span>
      ) : null}
      <button
        type="button"
        className="worker-install__legacy-btn"
        onClick={mint}
        disabled={state === "minting"}
      >
        {state === "minting" ? t("minting") : t("legacyMint")}
      </button>
    </div>
  );
}

type RowState = "idle" | "confirming" | "revoking" | "error";

/**
 * One registered worker, rendered as a card: name, the labels (if any), a
 * capability chip per CLI it can drive, an online/offline status pill (online =
 * a recent heartbeat), and Lift E4's last-seen + added-on detail. Revoke is
 * REAL and confirm-gated.
 */
function WorkerRow({
  worker,
  onRevoked,
  revoke,
}: {
  worker: Worker;
  onRevoked: () => void;
  revoke: (id: string) => Promise<void>;
}) {
  const [state, setState] = useState<RowState>("idle");
  const t = useTranslations("settings.models.workers");

  async function confirmRevoke() {
    if (state === "revoking") return;
    setState("revoking");
    try {
      await revoke(worker.id);
      onRevoked();
    } catch {
      setState("error");
    }
  }

  const online = worker.status === "online";
  const lastSeen = worker.last_heartbeat ? formatRelative(worker.last_heartbeat) : null;
  const createdOn = worker.created_at ? formatDate(worker.created_at) : null;

  return (
    <li className="worker-card">
      <div className="worker-card__body">
        <div className="worker-card__head">
          <span className="worker-card__name">{worker.name}</span>
          <span
            className={`worker-card__pill${online ? " worker-card__pill--online" : ""}`}
            title={t("statusTitle")}
          >
            {online ? t("online") : t("offline")}
          </span>
        </div>
        <div className="worker-card__caps" aria-label={t("capabilitiesLabel")}>
          {worker.capabilities.map((cap) => (
            <span key={cap} className="worker-card__chip">
              {cap}
            </span>
          ))}
        </div>
        {worker.labels.length > 0 ? (
          <p className="worker-card__labels">{worker.labels.join(" · ")}</p>
        ) : null}
        {(lastSeen || createdOn) && (
          <p className="worker-card__detail">
            {lastSeen ? <>{t("lastSeen", { when: lastSeen })}</> : null}
            {lastSeen && createdOn ? " · " : null}
            {createdOn ? <>{t("addedOn", { when: createdOn })}</> : null}
          </p>
        )}
      </div>

      <div className="worker-card__actions">
        {state === "error" ? (
          <span className="worker-card__error" aria-live="polite">
            {t("revokeError")}
          </span>
        ) : null}

        {state === "confirming" || state === "revoking" ? (
          <>
            <button
              type="button"
              className="worker-card__danger"
              onClick={confirmRevoke}
              disabled={state === "revoking"}
            >
              {state === "revoking" ? t("revoking") : t("confirmRevoke")}
            </button>
            <button
              type="button"
              className="worker-card__ghost"
              onClick={() => setState("idle")}
              disabled={state === "revoking"}
            >
              {t("cancel")}
            </button>
          </>
        ) : (
          <button
            type="button"
            className="worker-card__revoke"
            onClick={() => setState("confirming")}
          >
            {t("revoke")}
          </button>
        )}
      </div>
    </li>
  );
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString();
}

function formatRelative(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const diffMs = Date.now() - d.getTime();
  const min = Math.round(diffMs / 60_000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  return `${day}d ago`;
}
