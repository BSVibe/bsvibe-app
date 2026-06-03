"use client";

import { getProductBootstrap as realGetBootstrap } from "@/lib/api/products";
import type { ProductBootstrap } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

/** Polling interval for the bootstrap progress endpoint while the job is in
 *  flight. 2s feels live without spamming; the calm UI design intentionally
 *  avoids a spinner so the cadence doesn't need to be sub-second. */
const POLL_INTERVAL_MS = 2000;

/** Lift A v2 — calm progress panel for a Product whose `bootstrap_status`
 *  is non-null and not yet `complete`. Shows one line of prose per lifecycle
 *  stage; updates on poll. Disappears entirely on `complete`; switches to
 *  an amber failure variant on any `failed:*` status.
 *
 *  Mounted unconditionally; renders `null` when there's nothing to show
 *  (no bootstrap row, or bootstrap already complete) so the parent doesn't
 *  need to guard. */
export default function BootstrapStatusPanel({
  productId,
  getBootstrap = realGetBootstrap,
  pollIntervalMs = POLL_INTERVAL_MS,
}: {
  productId: string;
  getBootstrap?: (id: string) => Promise<ProductBootstrap>;
  pollIntervalMs?: number;
}) {
  const t = useTranslations("products.bootstrap");
  const [progress, setProgress] = useState<ProductBootstrap | null>(null);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function tick() {
      try {
        const next = await getBootstrap(productId);
        if (!active) return;
        setProgress(next);
        if (next.status && !isTerminal(next.status)) {
          timer = setTimeout(tick, pollIntervalMs);
        }
      } catch {
        // Calm: swallow transient fetch errors; the next render still shows
        // the last known state.
      }
    }
    tick();
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
  }, [productId, getBootstrap, pollIntervalMs]);

  if (!progress || !progress.status) return null;
  if (progress.status === "complete") return null;

  const isFailed = progress.status.startsWith("failed:");
  const message = messageFor(progress.status, t);

  return (
    <section
      className={
        isFailed
          ? "bootstrap-status bootstrap-status--failed"
          : "bootstrap-status bootstrap-status--running"
      }
      aria-live="polite"
      aria-label="bootstrap progress"
    >
      <p className="bootstrap-status__line">{message}</p>
      {isFailed && progress.error ? (
        <p className="bootstrap-status__detail">
          <span className="bootstrap-status__detail-label">{t("errorPrefix")}</span>
          <span className="bootstrap-status__detail-text">{progress.error}</span>
        </p>
      ) : null}
    </section>
  );
}

function isTerminal(status: string): boolean {
  return status === "complete" || status.startsWith("failed:");
}

function messageFor(status: string, t: (key: string) => string): string {
  switch (status) {
    case "pending":
      return t("pending");
    case "cloning":
      return t("cloning");
    case "analyzing":
      return t("analyzing");
    case "ingesting":
      return t("ingesting");
    case "failed:clone":
      return t("failedClone");
    case "failed:too_large":
      return t("failedTooLarge");
    case "failed:ingest":
      return t("failedIngest");
    default:
      // Unknown future status — render the analyzing-style message rather
      // than nothing, so a backend ahead of the UI doesn't go silent.
      return t("analyzing");
  }
}
