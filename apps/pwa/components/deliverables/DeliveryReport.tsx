"use client";

import { ApiError } from "@/lib/api/client";
import {
  getDeliverableArtifact,
  getDeliverableDiff,
  getDeliverableReport,
} from "@/lib/api/deliverables";
import type {
  ArtifactContent,
  DeliverableReport,
  VerificationOutcome,
  VerificationReportItem,
} from "@/lib/api/types";
import {
  langFromFileName,
  splitUnifiedDiffByFile,
  synthesizeAdditionHunk,
} from "@/lib/diff/diffData";
import { conciseSummary } from "@/lib/text/summary";
import { useResolvedTheme } from "@/lib/theme/useTheme";
import { DiffModeEnum, DiffView as GitDiffView } from "@git-diff-view/react";
import "@git-diff-view/react/styles/diff-view.css";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

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

// The backend stamps verification checks with a few FIXED English rationale
// strings (the L1 quality gate, the retrieved-knowledge marker incl. its legacy
// "BSage" wording). They reached the Korean UI untranslated — map the known ones
// to i18n; a free-form (agent-authored) rationale falls back to its raw text.
const _RATIONALE_KEY: Record<string, string> = {
  "Mandatory project quality gate — enforced on the changed files": "mandatoryGateRationale",
  "Canonical patterns retrieved for this change": "retrievedKnowledgeRationale",
  "BSage canonical patterns retrieved for this change": "retrievedKnowledgeRationale",
};

function VerificationBlock({ verification }: { verification: VerificationReportItem }) {
  const t = useTranslations("report");
  const rationaleLabel = (raw: string): string => {
    const key = _RATIONALE_KEY[raw];
    return key ? t(key) : raw;
  };
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
                <span className="report-checks__rationale">{rationaleLabel(check.rationale)}</span>
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

/** "What was built" — the document centerpiece, a GitHub PR-review split: a left
 *  FILE LIST + a right DIFF PANEL. The first artifact opens by default; selecting
 *  another file refetches ONLY the right panel (the file list persists, the
 *  current marker moves) so a many-file deliverable stays scannable without
 *  re-rendering the whole document. The rail appears only when there's more than
 *  one file — a lone file stays a calm single panel. A deliverable with neither
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
  // The run's captured old↔new diff (product runs), split into a path→raw
  // per-file `git diff` section. `null` while loading; an empty map = nothing
  // captured (Direct run / a pre-feature row) → every file falls back to the
  // additions render. Best-effort: a 404 / read failure degrades to the empty
  // map, never an error wall.
  const [diffMap, setDiffMap] = useState<Map<string, string> | null>(null);

  useEffect(() => {
    let active = true;
    setDiffMap(null);
    getDeliverableDiff(id)
      .then((res) => {
        if (active)
          setDiffMap(typeof res.diff === "string" ? splitUnifiedDiffByFile(res.diff) : new Map());
      })
      .catch(() => {
        if (active) setDiffMap(new Map());
      });
    return () => {
      active = false;
    };
  }, [id]);

  if (artifact_refs.length === 0 && !artifact_uri) {
    return <p className="report-doc__muted">{t("noArtifacts")}</p>;
  }

  const showFiles = artifact_refs.length > 1;
  const selectedHunk = selected !== null ? (diffMap?.get(selected) ?? null) : null;
  return (
    <div className={`report-doc__built${showFiles ? " report-built--split" : ""}`}>
      {showFiles && (
        <nav className="report-built__files" aria-label={t("files")}>
          <ul className="report-built__file-list">
            {artifact_refs.map((ref) => {
              const active = selected === ref;
              return (
                <li key={ref}>
                  <button
                    type="button"
                    className={`report-built__file${active ? " report-built__file--active" : ""}`}
                    aria-current={active ? "true" : undefined}
                    onClick={() => setSelected(ref)}
                    title={ref}
                  >
                    {ref}
                  </button>
                </li>
              );
            })}
          </ul>
        </nav>
      )}
      <div className="report-built__panel">
        {selected &&
          (diffMap === null ? (
            <p className="report-artifact-view__loading" aria-busy="true">
              {t("artifactLoading")}
            </p>
          ) : selectedHunk ? (
            <HighlightedDiff fileName={selected} hunk={selectedHunk} />
          ) : (
            <FileContentPanel deliverableId={id} fileName={selected} hasDiff={hasDiff} />
          ))}
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
    </div>
  );
}

/** The shared diff pane — a `@git-diff-view/react` unified view of one file's
 *  `hunk` (a real captured diff, or a synthesized all-additions hunk for a
 *  no-before file). Syntax-highlighted, line-wrapped (no horizontal scroll), and
 *  themed to the document; the surrounding `report-diffview` owns the scroll so a
 *  tall file never reflows the page. */
function HighlightedDiff({ fileName, hunk }: { fileName: string; hunk: string }) {
  const theme = useResolvedTheme();
  const lang = langFromFileName(fileName);
  return (
    <div className="report-diffview">
      <GitDiffView
        data={{
          oldFile: { fileName, fileLang: lang },
          newFile: { fileName, fileLang: lang },
          hunks: [hunk],
        }}
        diffViewMode={DiffModeEnum.Unified}
        diffViewHighlight
        diffViewWrap
        diffViewTheme={theme}
        diffViewFontSize={13}
      />
    </div>
  );
}

type ArtifactLoaded =
  | { state: "loading" }
  | { state: "unavailable" }
  | { state: "ready"; artifact: ArtifactContent };

/** The right panel for a file with NO captured diff — fetches the produced
 *  CONTENT and renders it as syntax-highlighted additions (every line new, since
 *  there is no "before"). A 404 (cleaned run dir) degrades to a calm note that
 *  points at the git diff; a binary file shows its metadata note; a truncated
 *  file shows a note above the content. */
function FileContentPanel({
  deliverableId,
  fileName,
  hasDiff,
}: {
  deliverableId: string;
  fileName: string;
  hasDiff: boolean;
}) {
  const t = useTranslations("report");
  const [loaded, setLoaded] = useState<ArtifactLoaded>({ state: "loading" });

  useEffect(() => {
    let active = true;
    setLoaded({ state: "loading" });
    getDeliverableArtifact(deliverableId, fileName)
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
  }, [deliverableId, fileName]);

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
  if (artifact.binary) {
    return <p className="report-artifact-view__binary">{artifact.content}</p>;
  }
  // A freshly produced Markdown doc reads best RENDERED (headings, lists, bold)
  // — a code-diff with `+` gutters is a developer metaphor that confuses a
  // non-developer. An EDITED Markdown file still shows the diff (handled by the
  // caller's captured-diff branch); only a no-before doc reaches here.
  const isMarkdown = langFromFileName(fileName) === "markdown";
  return (
    <>
      {artifact.truncated && (
        <p className="report-artifact-view__truncated">{t("artifactTruncated")}</p>
      )}
      {isMarkdown ? (
        <MarkdownDoc content={artifact.content} />
      ) : (
        <HighlightedDiff
          fileName={fileName}
          hunk={synthesizeAdditionHunk(fileName, artifact.content)}
        />
      )}
    </>
  );
}

/** A no-before Markdown deliverable rendered for READING (GitHub-Flavored
 *  Markdown via remark-gfm). react-markdown is safe by default — raw HTML in the
 *  content is NOT rendered — so an agent-authored doc can't inject markup. */
function MarkdownDoc({ content }: { content: string }) {
  return (
    <div className="report-markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </div>
  );
}
