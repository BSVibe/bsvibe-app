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
  | { kind: "stalled"; result: ReindexResult }
  | { kind: "failed"; message: string };

/** Hard stop for the pass loop. The continue/stop decision comes from a number
 *  the SERVER supplies, so a stuck backend that always reports work remaining
 *  would otherwise spin the browser forever. At the server's per-pass cap this
 *  covers a corpus far larger than any real vault; hitting it means something
 *  is wrong, and the UI says so instead of hanging. */
const MAX_PASSES = 100;

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
      // One request is a BOUNDED pass, so a single call finishes the corpus
      // only when it is already small. Keep going while work remains and
      // report the running total — otherwise a 250-note corpus reports "100
      // embedded" and reads as a completed rebuild.
      let total: ReindexResult = {
        scanned: 0,
        embedded: 0,
        already: 0,
        disabled: false,
        remaining: 0,
      };
      let passes = 0;
      for (;;) {
        passes += 1;
        const pass = await reindexEmbeddings();
        if (pass.disabled) {
          total = pass;
          break;
        }
        total = {
          scanned: total.scanned + pass.scanned,
          embedded: total.embedded + pass.embedded,
          already: total.already + pass.already,
          disabled: false,
          remaining: pass.remaining,
        };
        if (pass.remaining === 0) break;
        if (passes >= MAX_PASSES) {
          setState({ kind: "stalled", result: total });
          return;
        }
      }
      setState({ kind: "done", result: total });
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

      {state.kind === "stalled" && (
        <p className="general-tab__error" role="alert">
          {t("stalled", { embedded: state.result.embedded })}
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
