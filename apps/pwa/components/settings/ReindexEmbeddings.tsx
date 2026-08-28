"use client";

import { reindexEmbeddings } from "@/lib/api/knowledge";
import type { ReindexResult } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useRef, useState } from "react";

/**
 * Settings → Developer → rebuild the search index.
 *
 * The note vector index is maintained event-driven by the settle promote hook,
 * which means it only heals when knowledge activity happens to occur. That is
 * fine in steady state and useless after a correction lands during a quiet
 * stretch: measured on prod 2026-08-28, 1,724 rows carried no content
 * fingerprint and no run had happened in 30 hours to trigger a pass. The
 * backfill was correct and had no surface anyone could press — its only
 * deliberate trigger was a REST route whose callers were tests.
 *
 * The report distinguishes three outcomes rather than collapsing them into
 * "done": real counts, an explicit "no embedding model configured" (where the
 * zeros mean *did not look*), and a failure that says so.
 */
type State =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "done"; result: ReindexResult }
  | { kind: "failed"; message: string };

export default function ReindexEmbeddings() {
  const t = useTranslations("settings.developer.reindex");
  const [state, setState] = useState<State>({ kind: "idle" });
  // A REF, not the state: the `disabled` attribute only exists after a
  // re-render, and `state` read from the render closure is still "idle" for a
  // second click dispatched in the same tick — so neither can stop a real
  // double-click. A pass embeds the whole corpus, so that second fire is
  // expensive rather than merely redundant.
  const inFlight = useRef(false);

  async function run() {
    if (inFlight.current) return;
    inFlight.current = true;
    setState({ kind: "running" });
    try {
      setState({ kind: "done", result: await reindexEmbeddings() });
    } catch (e) {
      setState({ kind: "failed", message: e instanceof Error ? e.message : String(e) });
    } finally {
      inFlight.current = false;
    }
  }

  return (
    <section className="account-section" aria-label={t("title")}>
      <header className="developer-tab__header">
        <h2 className="section-label">{t("title")}</h2>
        <button
          type="button"
          className="developer-tab__add"
          onClick={run}
          disabled={state.kind === "running"}
        >
          {state.kind === "running" ? t("running") : t("button")}
        </button>
      </header>

      <p className="general-tab__hint">{t("lede")}</p>

      {state.kind === "done" && state.result.disabled && (
        <p className="general-tab__hint" aria-live="polite">
          {t("disabled")}
        </p>
      )}

      {state.kind === "done" && !state.result.disabled && (
        <p className="general-tab__hint" aria-live="polite">
          {t("result", {
            scanned: state.result.scanned,
            embedded: state.result.embedded,
            already: state.result.already,
          })}
        </p>
      )}

      {state.kind === "failed" && (
        <p className="general-tab__error" role="alert">
          {t("error", { message: state.message })}
        </p>
      )}
    </section>
  );
}
