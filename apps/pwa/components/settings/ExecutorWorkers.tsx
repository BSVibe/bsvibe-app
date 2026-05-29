"use client";

import { backendBaseUrl } from "@/lib/api/client";
import type { Worker } from "@/lib/api/types";
import { listWorkers, mintInstallToken, revokeWorker } from "@/lib/api/workers";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import CopyField from "./CopyField";

/**
 * Settings → Models → "Executor workers" (executor-pool design's "subscription
 * accounts"). The founder registers / views / revokes the machines that run the
 * BSVibe worker process, where their coding-agent CLIs (claude_code / codex /
 * opencode) are signed in. Registering one lets BSVibe route work to those CLIs
 * under the founder's own subscription, alongside the API model accounts above.
 *
 * Backed by the REAL /api/v1/workers endpoints (backend/api/v1/workers.py):
 *
 *  - List   ← GET    /api/v1/workers              (status + capabilities; the
 *                                                  response carries no exact
 *                                                  last-seen yet — show the
 *                                                  heartbeat-driven status pill)
 *  - Connect → POST  /api/v1/workers/install-token (mints a one-time install
 *                                                  token, revealed ONCE; the
 *                                                  founder runs the worker
 *                                                  process with it)
 *  - Revoke → DELETE /api/v1/workers/{id}          (confirm-gated; 204)
 *
 * The list loads on mount and re-reads after a successful revoke so the section
 * always reflects the server. A failed list read degrades to a calm inline note
 * rather than a blanked page. The connect flow's own write (mint) is independent
 * of the list's read.
 *
 * Deferred (later lifts): the tier→model ROUTING section, and an exact last-seen
 * timestamp (a later backend tweak — there is no `last_heartbeat` in the
 * response yet).
 */
type ListState = { data: Worker[]; failed: boolean } | null;
type MintState = "idle" | "minting" | "error";

export default function ExecutorWorkers() {
  const [list, setList] = useState<ListState>(null);
  const [mintState, setMintState] = useState<MintState>("idle");
  const [token, setToken] = useState<string | null>(null);
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

  async function connect() {
    if (mintState === "minting") return;
    setMintState("minting");
    try {
      const minted = await mintInstallToken();
      setToken(minted.token);
      setMintState("idle");
    } catch {
      setMintState("error");
    }
  }

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

      {/* CONNECT — mint + one-time install-token reveal. After a successful mint
          we show ONLY the token panel until the founder dismisses it, so the
          one-time capability is the focus. */}
      {token ? (
        <section className="worker-token" aria-label={t("credentialsLabel")}>
          <p className="worker-token__title">{t("tokenTitle")}</p>
          <p className="worker-token__warn">{t("tokenWarn")}</p>

          <CopyField label={t("tokenLabel")} value={token} secret />

          <p className="worker-token__run-title">{t("runTitle")}</p>
          <CopyField
            label={t("runCommandLabel")}
            value={t("runCommand", { token, serverUrl: backendBaseUrl() })}
            secret
          />
          <p className="worker-token__hint">{t("runHint")}</p>

          <button type="button" className="worker-token__done" onClick={() => setToken(null)}>
            {t("done")}
          </button>
        </section>
      ) : (
        <div className="workers__connect">
          {mintState === "error" ? (
            <span className="workers__error" aria-live="polite">
              {t("mintError")}
            </span>
          ) : null}
          <button
            type="button"
            className="workers__connect-btn"
            onClick={connect}
            disabled={mintState === "minting"}
          >
            {mintState === "minting" ? t("minting") : t("connect")}
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

type RowState = "idle" | "confirming" | "revoking" | "error";

/**
 * One registered worker, rendered as a card: name, the labels (if any), a
 * capability chip per CLI it can drive, and an online/offline status pill
 * (online = a recent heartbeat). Revoke is REAL and confirm-gated: the first
 * "Revoke" reveals a "Confirm revoke" (+ "Cancel") so a revoke is never a single
 * stray tap; Confirm fires the DELETE and `onRevoked` re-reads the list. A
 * failed revoke shows a calm inline note and keeps the card actionable.
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
      // The container re-read replaces this card; leave it in revoking so the
      // button can't be re-fired before then.
    } catch {
      setState("error");
    }
  }

  const online = worker.status === "online";

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
