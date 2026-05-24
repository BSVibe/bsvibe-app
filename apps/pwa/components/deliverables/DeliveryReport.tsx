"use client";

import { ApiError } from "@/lib/api/client";
import { getDeliverableReport } from "@/lib/api/deliverables";
import type {
  DeliverableReport,
  VerificationOutcome,
  VerificationReportItem,
} from "@/lib/api/types";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useState } from "react";

/**
 * Delivery Report — the "glass box proof" for one shipped deliverable
 * (Stitch "Delivery Report — Code"). Read-only: the artifact summary, the
 * verdict, "How BSVibe checked this" (the declared verification contract +
 * the result of running it), the artifacts (refs + external link), and a link
 * out to the diff.
 *
 * Composed client-side from REAL `GET /api/v1/deliverables/{id}/report`. The
 * contract/result JSON is free-form (shape varies by verifier), so it is
 * rendered DEFENSIVELY — an odd shape degrades to a calm line, never a crash.
 * States mirror the product-detail surface: loading / not-found (404, NOT an
 * error) / error / ready. A run with no verification shows a calm
 * "no verification recorded" note rather than erroring.
 *
 * Deferred (no data behind them yet): a full inline side-by-side diff VIEWER
 * (we link out to diff_url), any risk score the model doesn't carry, and
 * per-deliverable approve / follow-up actions (the report is read-only proof).
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

      {loaded.state === "ready" && <ReportBody report={loaded.report} />}
    </div>
  );
}

function ReportBody({ report }: { report: DeliverableReport }) {
  const t = useTranslations("report");
  const { deliverable, verifications } = report;
  const summary = deliverable.summary?.trim() || t("untitled");

  return (
    <article className="report__body">
      <header className="report__header">
        <h1 className="report__title">{summary}</h1>
      </header>

      <Verdict verifications={verifications} />

      <section className="report-checks" aria-label={t("howChecked")}>
        <h2 className="section-label">{t("howChecked")}</h2>
        {verifications.length === 0 ? (
          <p className="report-checks__empty">{t("noVerification")}</p>
        ) : (
          verifications.map((v) => <VerificationBlock key={v.id} verification={v} />)
        )}
      </section>

      <Artifacts deliverable={deliverable} />

      {deliverable.diff_url && (
        <section className="report-diff" aria-label={t("diff")}>
          <h2 className="section-label">{t("diff")}</h2>
          <a
            className="report-diff__link"
            href={deliverable.diff_url}
            target="_blank"
            rel="noopener noreferrer"
          >
            {t("viewDiff")}
          </a>
        </section>
      )}
    </article>
  );
}

/** Verdict tone + label from the strongest recorded outcome (failed beats
 *  inconclusive beats passed). No verification → an honest "not yet verified". */
function Verdict({ verifications }: { verifications: VerificationReportItem[] }) {
  const t = useTranslations("report");
  const outcome = strongestOutcome(verifications);
  const tone = outcome ?? "none";
  return (
    <section className={`report-verdict report-verdict--${tone}`} aria-label={t("verdict")}>
      <span className="report-verdict__label">{t(`verdictLabel.${tone}`)}</span>
    </section>
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
 *  if present, else a compact count, else nothing. Never throws. */
function summarizeResult(result: Record<string, unknown>): string | null {
  for (const key of ["summary", "output", "error", "detail"]) {
    const value = result[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

function Artifacts({
  deliverable,
}: {
  deliverable: DeliverableReport["deliverable"];
}) {
  const t = useTranslations("report");
  const { artifact_refs, artifact_uri } = deliverable;
  return (
    <section className="report-artifacts" aria-label={t("artifacts")}>
      <h2 className="section-label">{t("artifacts")}</h2>
      {artifact_refs.length === 0 && !artifact_uri ? (
        <p className="report-artifacts__empty">{t("noArtifacts")}</p>
      ) : (
        <>
          {artifact_refs.length > 0 && (
            <ul className="report-artifacts__list">
              {artifact_refs.map((ref) => (
                <li key={ref} className="report-artifacts__row">
                  {ref}
                </li>
              ))}
            </ul>
          )}
          {artifact_uri && (
            <a
              className="report-artifacts__link"
              href={artifact_uri}
              target="_blank"
              rel="noopener noreferrer"
            >
              {t("openArtifact")}
            </a>
          )}
        </>
      )}
    </section>
  );
}
