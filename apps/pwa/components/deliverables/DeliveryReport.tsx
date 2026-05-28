"use client";

import { ApiError } from "@/lib/api/client";
import { getDeliverableArtifact, getDeliverableReport } from "@/lib/api/deliverables";
import type {
  ArtifactContent,
  DeliverableReport,
  VerificationOutcome,
  VerificationReportItem,
} from "@/lib/api/types";
import { conciseSummary } from "@/lib/text/summary";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useState } from "react";

/**
 * Delivery Report — the "glass box proof" for one shipped deliverable, read as a
 * single Notion-like DOCUMENT (Stitch "Delivery Report" redesign): a masthead
 * (title + type / verdict chips + date), then the founder's "Your request", the
 * prominent "What was built" (the produced file CONTENT shown inline by default,
 * switchable across artifacts), "How BSVibe checked this" (the verdict + the
 * declared verification contract and the result of running it), and a diff link.
 *
 * Composed client-side from REAL `GET /api/v1/deliverables/{id}/report` — which
 * now also carries `request` (the founder's Direction, from the producing run).
 * The contract/result JSON is free-form (shape varies by verifier), so it is
 * rendered DEFENSIVELY — an odd shape degrades to a calm line, never a crash.
 * States: loading / not-found (404, NOT an error) / error / ready.
 *
 * "What was built" auto-opens the first artifact's CONTENT (the document's
 * centerpiece — the founder SEES the produced file without a click). A 404 /
 * cleaned run dir degrades to a calm "couldn't show this file — see the diff"
 * note; binary files surface metadata only; a truncated file shows a note.
 *
 * Read-only proof: no approve / follow-up actions here (those live on Decisions
 * and have no per-report endpoint), and no risk score the model doesn't carry.
 */
type Loaded =
  | { state: "loading" }
  | { state: "error" }
  | { state: "not-found" }
  | { state: "ready"; report: DeliverableReport };

/** One declared check, normalized for display from the free-form contract. */
interface DisplayCheck {
  label: string;
  rationale: string;
}

/** Pull the declared checks out of a free-form contract, tolerantly. Mirrors
 *  the backend VerificationContract shape ({checks:[{kind,command|criteria,
 *  rationale}]}) but never throws on an odd shape. */
function checksFromContract(contract: Record<string, unknown>): DisplayCheck[] {
  const raw = contract.checks;
  if (!Array.isArray(raw)) return [];
  const out: DisplayCheck[] = [];
  for (const item of raw) {
    if (typeof item !== "object" || item === null) continue;
    const check = item as Record<string, unknown>;
    const rationale = typeof check.rationale === "string" ? check.rationale : "";
    if (check.kind === "command" && typeof check.command === "string" && check.command.trim()) {
      out.push({ label: check.command, rationale });
    } else if (check.kind === "judge" && Array.isArray(check.criteria)) {
      for (const c of check.criteria) {
        const text = String(c).trim();
        if (text) out.push({ label: text, rationale });
      }
    }
  }
  return out;
}

export default function DeliveryReport({ deliverableId }: { deliverableId: string }) {
  const [loaded, setLoaded] = useState<Loaded>({ state: "loading" });
  const t = useTranslations("report");

  useEffect(() => {
    let active = true;
    setLoaded({ state: "loading" });
    getDeliverableReport(deliverableId)
      .then((report) => {
        if (active) setLoaded({ state: "ready", report });
      })
      .catch((error: unknown) => {
        if (!active) return;
        // A 404 (deliverable not in this workspace / unknown id) is a calm
        // not-found, NOT an error wall.
        if (error instanceof ApiError && error.status === 404) {
          setLoaded({ state: "not-found" });
        } else {
          setLoaded({ state: "error" });
        }
      });
    return () => {
      active = false;
    };
  }, [deliverableId]);

  return (
    <div className="report">
      <Link className="report__back" href="/brief">
        {t("back")}
      </Link>

      {loaded.state === "loading" && (
        <p className="report__loading-note" aria-busy="true">
          {t("loadingNote")}
        </p>
      )}

      {loaded.state === "not-found" && (
        <section className="report-empty" aria-label={t("region")}>
          <p className="report-empty__line">{t("notFoundLine")}</p>
          <p className="report-empty__sub">
            <Link href="/brief">{t("backToBrief")}</Link>
          </p>
        </section>
      )}

      {loaded.state === "error" && (
        <section className="report-empty" aria-label={t("region")}>
          <p className="report-empty__line">{t("errorLine")}</p>
          <p className="report-empty__sub">{t("errorSub")}</p>
        </section>
      )}

      {loaded.state === "ready" && <ReportDocument report={loaded.report} />}
    </div>
  );
}

function ReportDocument({ report }: { report: DeliverableReport }) {
  const t = useTranslations("report");
  const { deliverable, request, verified, verifications } = report;
  // Defensive: an older / malformed payload may omit references — degrade to an
  // empty list (the section simply doesn't render), never a crash.
  const references = report.references ?? [];
  // Concise document title — the first sentence of the (often paragraph-long)
  // LLM summary, not the whole blob; the detail lives in the body + request.
  const summary = conciseSummary(deliverable.summary, t("untitled"));
  // B4 defense-in-depth: the green "This is verified" verdict ("passed" tone)
  // shows ONLY when the backend's authoritative `verified` flag is set (a real
  // PASSED VerificationResult). If a stray verification row claims "passed" but
  // the backend did not certify the deliverable, the verdict reads honestly as
  // "Not yet verified" ("none") rather than a hollow green.
  const tone = verdictTone(verified, verifications);
  const hasDiff = Boolean(deliverable.diff_url);

  return (
    <article className="report-doc">
      <header className="report-doc__head">
        <div className="report-doc__chips">
          <span className="report-doc__chip">{t(`typeLabel.${deliverable.deliverable_type}`)}</span>
          <span className={`report-doc__chip report-doc__chip--${tone}`}>
            {t(`verdictLabel.${tone}`)}
          </span>
          <span className="report-doc__date">{deliverable.created_at.slice(0, 10)}</span>
        </div>
        <h1 className="report-doc__title">{summary}</h1>
      </header>

      {request && (
        <section className="report-doc__section" aria-label={t("yourRequest")}>
          <h2 className="report-doc__label">{t("yourRequest")}</h2>
          <blockquote className="report-doc__request">{request}</blockquote>
        </section>
      )}

      <section className="report-doc__section" aria-label={t("whatWasBuilt")}>
        <h2 className="report-doc__label">{t("whatWasBuilt")}</h2>
        <WhatWasBuilt deliverable={deliverable} hasDiff={hasDiff} />
      </section>

      {references.length > 0 && (
        <section className="report-doc__section" aria-label={t("referenced")}>
          <h2 className="report-doc__label">{t("referenced")}</h2>
          <p className="report-doc__muted">{t("referencedHint")}</p>
          <ul className="report-doc__references">
            {references.map((reference, i) => (
              // References are free-form statements that may repeat across
              // re-attempts (deduped server-side); index keys the row.
              <li key={`${i}-${reference}`} className="report-doc__reference">
                {reference}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="report-doc__section" aria-label={t("howChecked")}>
        <h2 className="report-doc__label">{t("howChecked")}</h2>
        <p className={`report-doc__verdict report-doc__verdict--${tone}`}>
          {t(`verdictLabel.${tone}`)}
        </p>
        {verifications.length === 0 ? (
          <p className="report-doc__muted">{t("noVerification")}</p>
        ) : (
          verifications.map((v) => <VerificationBlock key={v.id} verification={v} />)
        )}
      </section>

      {deliverable.diff_url && (
        <a
          className="report-doc__diff"
          href={deliverable.diff_url}
          target="_blank"
          rel="noopener noreferrer"
        >
          {t("viewDiff")}
        </a>
      )}
    </article>
  );
}

/** failed > inconclusive > passed; null when there is no verification. */
function strongestOutcome(verifications: VerificationReportItem[]): VerificationOutcome | null {
  if (verifications.length === 0) return null;
  const outcomes = new Set(verifications.map((v) => v.outcome));
  if (outcomes.has("failed")) return "failed";
  if (outcomes.has("inconclusive")) return "inconclusive";
  return "passed";
}

/** The verdict tone for the masthead/verdict line, gated by the backend's
 *  authoritative `verified` flag (B4). The green "passed" tone (= "This is
 *  verified") is shown ONLY when `verified` is true; otherwise the strongest
 *  honest non-passed signal is surfaced (failed / inconclusive), degrading to
 *  "none" (Not yet verified) when nothing certifies the deliverable. This means
 *  a stray "passed" row can never produce a green badge the backend didn't
 *  certify. */
function verdictTone(
  verified: boolean,
  verifications: VerificationReportItem[],
): VerificationOutcome | "none" {
  if (verified) return "passed";
  const strongest = strongestOutcome(verifications);
  // Drop a "passed" the backend did not certify back to the honest "none".
  if (strongest === null || strongest === "passed") return "none";
  return strongest;
}

function VerificationBlock({ verification }: { verification: VerificationReportItem }) {
  const t = useTranslations("report");
  const checks = checksFromContract(verification.contract);
  const resultSummary = summarizeResult(verification.result);
  return (
    <div className="report-checks__block">
      {checks.length === 0 ? (
        <p className="report-checks__none">{t("noChecksDeclared")}</p>
      ) : (
        <ul className="report-checks__list">
          {checks.map((check, i) => (
            <li
              // Checks are positional, free-form, and may repeat — index keys the row.
              key={`${verification.id}-${i}`}
              className="report-checks__row"
            >
              <span className="report-checks__cmd">{check.label}</span>
              {check.rationale && (
                <span className="report-checks__rationale">{check.rationale}</span>
              )}
            </li>
          ))}
        </ul>
      )}
      {resultSummary && <p className="report-checks__result">{resultSummary}</p>}
    </div>
  );
}

/** A calm one-line summary of the free-form result JSON: a known string field
 *  if present, else nothing. Never throws. */
function summarizeResult(result: Record<string, unknown>): string | null {
  for (const key of ["summary", "output", "error", "detail"]) {
    const value = result[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

/** "What was built" — the document centerpiece. Shows the produced file CONTENT
 *  inline by default (the first artifact), switchable across the deliverable's
 *  refs; the external landing link when one exists. A deliverable with neither
 *  refs nor a uri shows a calm "nothing produced" note. */
function WhatWasBuilt({
  deliverable,
  hasDiff,
}: {
  deliverable: DeliverableReport["deliverable"];
  hasDiff: boolean;
}) {
  const t = useTranslations("report");
  const { id, artifact_refs, artifact_uri } = deliverable;
  const [selected, setSelected] = useState<string | null>(artifact_refs[0] ?? null);

  if (artifact_refs.length === 0 && !artifact_uri) {
    return <p className="report-doc__muted">{t("noArtifacts")}</p>;
  }

  return (
    <div className="report-doc__built">
      {artifact_refs.length > 1 && (
        <ul className="report-doc__tabs" aria-label={t("artifacts")}>
          {artifact_refs.map((ref) => (
            <li key={ref}>
              <button
                type="button"
                className={`report-doc__tab${selected === ref ? " report-doc__tab--active" : ""}`}
                aria-pressed={selected === ref}
                onClick={() => setSelected(ref)}
                title={ref}
              >
                {ref}
              </button>
            </li>
          ))}
        </ul>
      )}
      {selected && <ArtifactViewer deliverableId={id} refName={selected} hasDiff={hasDiff} />}
      {artifact_uri && (
        <a
          className="report-doc__open"
          href={artifact_uri}
          target="_blank"
          rel="noopener noreferrer"
        >
          {t("openArtifact")}
        </a>
      )}
    </div>
  );
}

type ArtifactLoaded =
  | { state: "loading" }
  | { state: "unavailable" }
  | { state: "ready"; artifact: ArtifactContent };

/** Inline content viewer for one artifact ref. Fetches the produced file
 *  CONTENT and shows it in a calm scrollable monospace block. A 404 (cleaned
 *  run dir / unavailable) degrades to a "couldn't show this file" note that
 *  points back at the git diff; any other failure shows the same calm note. A
 *  binary file shows its metadata note; a truncated file shows a note. */
function ArtifactViewer({
  deliverableId,
  refName,
  hasDiff,
}: {
  deliverableId: string;
  refName: string;
  hasDiff: boolean;
}) {
  const t = useTranslations("report");
  const [loaded, setLoaded] = useState<ArtifactLoaded>({ state: "loading" });

  useEffect(() => {
    let active = true;
    setLoaded({ state: "loading" });
    getDeliverableArtifact(deliverableId, refName)
      .then((artifact) => {
        if (active) setLoaded({ state: "ready", artifact });
      })
      .catch(() => {
        // 404 (file cleaned / not whitelisted) OR any read failure → the same
        // calm "couldn't show this file" fallback (never an error wall).
        if (active) setLoaded({ state: "unavailable" });
      });
    return () => {
      active = false;
    };
  }, [deliverableId, refName]);

  if (loaded.state === "loading") {
    return (
      <p className="report-artifact-view__loading" aria-busy="true">
        {t("artifactLoading")}
      </p>
    );
  }

  if (loaded.state === "unavailable") {
    return (
      <p className="report-artifact-view__unavailable">
        {hasDiff ? t("artifactUnavailableWithDiff") : t("artifactUnavailable")}
      </p>
    );
  }

  const { artifact } = loaded;
  return (
    <div className="report-artifact-view">
      <div className="report-artifact-view__head">
        <span className="report-artifact-view__path">{artifact.ref}</span>
      </div>
      {artifact.binary ? (
        <p className="report-artifact-view__binary">{artifact.content}</p>
      ) : (
        <>
          {artifact.truncated && (
            <p className="report-artifact-view__truncated">{t("artifactTruncated")}</p>
          )}
          <pre className="report-artifact-view__content">
            <code>{artifact.content}</code>
          </pre>
        </>
      )}
    </div>
  );
}
