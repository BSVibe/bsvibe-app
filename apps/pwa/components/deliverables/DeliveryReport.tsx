"use client";

import { relativeTime } from "@/components/decisions/relative-time";
import { ApiError } from "@/lib/api/client";
import {
  getDeliverableArtifact,
  getDeliverableDiff,
  getDeliverableReport,
  retractDeliverable,
} from "@/lib/api/deliverables";
import { getConceptDetail, getNote } from "@/lib/api/knowledge";
import { approveSafeModeItem, denySafeModeItem } from "@/lib/api/safemode";
import type {
  ArtifactContent,
  ConceptDetail,
  DeliverableReport,
  KnowledgeNote,
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
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Delivery Report (R3 redesign) — the "glass box proof" for one shipped
 * deliverable, read as a calm, editorial DOCUMENT. Top to bottom: a small
 * status pill (Verified / Needs review) + the plain title + a meta chip row,
 * then the LEAD "What this did" (the R1 plain-language narrative, falling back
 * to the founder's Direction), an OPTIONAL quiet "Note" (surfaced only on a
 * non-passed verification signal), "How it was verified" as a clean CHECKLIST,
 * a "Knowledge" group (referenced chips + a future-ready Learned group), the
 * captured diff DEMOTED behind a collapsed disclosure, and a de-emphasized
 * footer carrying the one mutating affordance (roll back).
 *
 * Composed client-side from REAL `GET /api/v1/deliverables/{id}/report` — which
 * carries `narrative` (R1), `request` (the founder's Direction), `references`
 * (knowledge read), and the verification contract/result. The contract/result
 * JSON is free-form (shape varies by verifier), so it is rendered DEFENSIVELY —
 * an odd shape degrades to a calm line, never a crash. States: loading /
 * not-found (404, NOT an error) / error / ready.
 *
 * The diff viewer is behind a collapsed disclosure (secondary, not shown
 * expanded on load). A 404 / cleaned run dir degrades to a calm "couldn't show
 * this file — see the diff" note; binary files surface metadata only; a
 * truncated file shows a note.
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

/** Note-level reference label (R8). The retriever folds garden-note references
 *  as free-form "Related note — garden/seedling/settle-<slug>.md" statements;
 *  there's no note viewer yet, so rather than show a raw internal path that
 *  looks clickable but isn't, surface a readable note title (de-slugged from the
 *  filename). Non-note statements (a prior decision / rejection) pass through. */
function prettyReference(reference: string): string {
  const match = reference.match(/^related note\s*[—–-]\s*(.+\.md)$/i);
  if (!match) return reference;
  const file = (match[1].split("/").pop() ?? match[1]).replace(/\.md$/i, "");
  const slug = file.replace(/^settle-/, "").trim();
  if (!slug) return reference;
  const title = slug.replace(/[-_]+/g, " ").trim();
  return title.charAt(0).toUpperCase() + title.slice(1);
}

/** The vault-relative note path inside a "Related note — <path>.md" reference,
 *  so the chip can deep-link to the note viewer (R12). `null` for a non-note
 *  reference (a prior decision/preference), which stays plain text. */
function referenceNotePath(reference: string): string | null {
  const match = reference.match(/^related note\s*[—–-]\s*(.+\.md)$/i);
  return match ? match[1].trim() : null;
}

// Prefixes the retrievers stamp on NON-concept references (note / prior decision
// / prior rejection). Everything else the CompositeCanonRetriever folds in is a
// CanonConceptRetriever statement — the concept's display name (R13).
const NON_CONCEPT_REFERENCE = [
  /^related note\s*[—–-]/i,
  /^prior decision\s*[—–-]/i,
  /^avoid \(prior rejection\)\s*[—–-]/i,
];

/** The concept id (vault slug) for a canon-concept reference, so the chip can
 *  deep-link to the concept viewer (R13). `null` for a note / decision /
 *  rejection statement, which is handled elsewhere or stays plain text. The
 *  canon retriever emits the concept's display name; its id is the slug. */
function referenceConceptId(reference: string): string | null {
  const text = reference.trim();
  if (!text || NON_CONCEPT_REFERENCE.some((re) => re.test(text))) return null;
  const slug = text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug || null;
}

/** A referenced-knowledge statement long enough to read as a SENTENCE (a canon
 *  statement / prior decision), not a short tag. A stadium pill (border-radius
 *  999px) mangles a multi-line sentence — its rounded ends push the first/last
 *  line's text outside the visible border — so these render as a readable block
 *  chip instead (`report-chip--statement`). Short concept-name chips stay pills. */
function isStatementReference(text: string): boolean {
  return text.trim().length > 60;
}

/** One command the verifier actually ran, parsed tolerantly from the free-form
 *  `result.command_results` blob (shape: {command, passed, exit_code, output}). */
interface CommandRun {
  command: string;
  passed: boolean;
  exitCode: number | null;
  output: string;
}

function commandRuns(result: Record<string, unknown>): CommandRun[] {
  const raw = result.command_results;
  if (!Array.isArray(raw)) return [];
  const out: CommandRun[] = [];
  for (const item of raw) {
    if (typeof item !== "object" || item === null) continue;
    const r = item as Record<string, unknown>;
    const command = typeof r.command === "string" ? r.command.trim() : "";
    if (!command) continue;
    out.push({
      command,
      passed: r.passed === true,
      exitCode: typeof r.exit_code === "number" ? r.exit_code : null,
      output: typeof r.output === "string" ? r.output : "",
    });
  }
  return out;
}

/** One command from the AUTHORITATIVE derived gate — the repo's OWN check the
 *  verifier ran (status ∈ passed | failed | unavailable). */
interface GateCommand {
  command: string;
  kind: string;
  status: string;
}

/** The repo's own verification gate, DERIVED by the verifier from the project's
 *  manifests and run as the authoritative check (backend `result.derived_gate`).
 *  This is what "How it was verified" should lead with — the agent's own declared
 *  commands (`command_results`) are only advisory. `null` for an older row that
 *  predates the derived gate, or a non-code run — the caller falls back to the
 *  declared-contract checklist. */
interface DerivedGate {
  applicable: boolean;
  passed: boolean;
  commands: GateCommand[];
}

function derivedGate(result: Record<string, unknown>): DerivedGate | null {
  const raw = result.derived_gate;
  if (typeof raw !== "object" || raw === null) return null;
  const g = raw as Record<string, unknown>;
  const rawCommands = Array.isArray(g.commands) ? g.commands : [];
  const commands: GateCommand[] = [];
  for (const item of rawCommands) {
    if (typeof item !== "object" || item === null) continue;
    const c = item as Record<string, unknown>;
    const command = typeof c.command === "string" ? c.command.trim() : "";
    if (!command) continue;
    commands.push({
      command,
      kind: typeof c.kind === "string" ? c.kind : "quality",
      status: typeof c.status === "string" ? c.status : "",
    });
  }
  return {
    applicable: g.applicable === true,
    passed: g.passed === true,
    commands,
  };
}

/** The I2 result-demonstration ("we ran it and saw this"), parsed tolerantly from
 *  `result.outcome_demonstration` ({verdict, probes:[{name,status}]}). `null` when
 *  no demonstration was recorded (older row / verifier didn't run one). */
interface Demonstration {
  verdict: string;
  probes: { name: string; status: string }[];
}

function demonstration(result: Record<string, unknown>): Demonstration | null {
  const raw = result.outcome_demonstration;
  if (typeof raw !== "object" || raw === null) return null;
  const d = raw as Record<string, unknown>;
  const verdict = typeof d.verdict === "string" ? d.verdict : "";
  if (!verdict) return null;
  const probes: { name: string; status: string }[] = [];
  const rawProbes = Array.isArray(d.probes) ? d.probes : [];
  for (const p of rawProbes) {
    if (typeof p !== "object" || p === null) continue;
    const pr = p as Record<string, unknown>;
    const name = typeof pr.name === "string" ? pr.name.trim() : "";
    if (name) probes.push({ name, status: typeof pr.status === "string" ? pr.status : "" });
  }
  return { verdict, probes };
}

/** The honesty grade (A–D) on a verification, or null when absent / malformed. */
function honestyGrade(v: VerificationReportItem): string | null {
  const g = v.honesty_grade;
  return typeof g === "string" && /^[A-D]$/.test(g.trim()) ? g.trim() : null;
}

/** The last few lines of a command's output — enough to see the failing assertion
 *  without dumping a whole log into the report. */
function tailOutput(output: string, lines = 6): string {
  const trimmed = output.replace(/\s+$/, "");
  if (!trimmed) return "";
  return trimmed.split("\n").slice(-lines).join("\n");
}

/** Split the run's verifications into the ONE authoritative result (the proof the
 *  founder should read) and the EARLIER attempts (superseded retries, collapsed
 *  behind a disclosure so a run that retried N times isn't a wall of red). The
 *  authoritative result is the latest PASSED one when the deliverable is verified,
 *  else the most recent attempt. `verifications` arrive oldest→newest. */
function splitVerifications(
  verifications: VerificationReportItem[],
  verified: boolean,
): { authoritative: VerificationReportItem | null; earlier: VerificationReportItem[] } {
  if (verifications.length === 0) return { authoritative: null, earlier: [] };
  let authIdx = verifications.length - 1;
  if (verified) {
    for (let i = verifications.length - 1; i >= 0; i--) {
      if (verifications[i].outcome === "passed") {
        authIdx = i;
        break;
      }
    }
  }
  return {
    authoritative: verifications[authIdx],
    earlier: verifications.filter((_, i) => i !== authIdx),
  };
}

export default function DeliveryReport({ deliverableId }: { deliverableId: string }) {
  const [loaded, setLoaded] = useState<Loaded>({ state: "loading" });
  // Bumped after a footer action (approve / decline a held delivery) so the
  // report re-reads and the footer reflects the new state (shipped → Rollback,
  // declined → nothing).
  const [reloadKey, setReloadKey] = useState(0);
  const t = useTranslations("report");

  // biome-ignore lint/correctness/useExhaustiveDependencies: reloadKey is a deliberate re-fetch trigger (bumped after a footer action), not read inside.
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
  }, [deliverableId, reloadKey]);

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

      {loaded.state === "ready" && (
        <ReportDocument report={loaded.report} onResolved={() => setReloadKey((k) => k + 1)} />
      )}
    </div>
  );
}

function ReportDocument({
  report,
  onResolved,
}: {
  report: DeliverableReport;
  onResolved: () => void;
}) {
  const t = useTranslations("report");
  const { deliverable, request, narrative, verified, verifications } = report;
  // R12 — the knowledge note open in the viewer panel (a 추가한/참고한 지식 chip
  // click), or null. The viewer fetches the note's content on open.
  const [openNote, setOpenNote] = useState<{ path: string; title: string } | null>(null);
  // R13 — the canon concept open in the concept viewer (a 참고한 지식 concept chip
  // click), or null.
  const [openConcept, setOpenConcept] = useState<{ id: string; label: string } | null>(null);
  // Defensive: an older / malformed payload may omit references / written —
  // degrade to empty lists (the group simply doesn't render), never a crash.
  const references = report.references ?? [];
  const written = report.written ?? [];
  // Concise document title — the first sentence of the (often paragraph-long)
  // LLM summary, not the whole blob; the detail lives in the narrative lead.
  const summary = conciseSummary(deliverable.summary, t("untitled"));
  // B4 defense-in-depth: the green "Verified" pill ("passed" tone) shows ONLY
  // when the backend's authoritative `verified` flag is set (a real PASSED
  // VerificationResult). A stray "passed" row the backend did not certify
  // reads honestly as "Needs review" ("none"), never a hollow green.
  const tone = verdictTone(verified, verifications);
  const hasDiff = Boolean(deliverable.diff_url);
  // The R1 plain-language "what this did" LEADS the document; when it's absent
  // (older row, narrative hiccup) we fall back to the founder's own Direction.
  const lead = narrative ?? request;
  // The strongest verification result drives the OPTIONAL Note: only a
  // non-passed outcome that carries a message surfaces as a quiet amber line.
  const noteMessage = nonPassedNote(verifications);
  const fileCount = deliverable.artifact_refs.length;
  const showKnowledge = references.length > 0 || written.length > 0;

  return (
    <article className="report-doc">
      <header className="report-doc__head">
        <span
          className={`report-status report-status--${tone === "passed" ? "verified" : "review"}`}
        >
          {tone === "passed" && (
            <svg
              className="report-status__check"
              viewBox="0 0 16 16"
              width="13"
              height="13"
              aria-hidden="true"
            >
              <path
                d="M13 4.5 6.5 11 3 7.5"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          )}
          {t(tone === "passed" ? "statusVerified" : "statusReview")}
        </span>
        <h1 className="report-doc__title">{summary}</h1>
        <div className="report-doc__meta">
          <span className="report-doc__meta-chip">
            {t(`typeLabel.${deliverable.deliverable_type}`)}
          </span>
          <span className="report-doc__meta-dot" aria-hidden="true">
            ·
          </span>
          <span className="report-doc__meta-time">{relativeTime(deliverable.created_at, t)}</span>
        </div>
      </header>

      {lead && (
        <section className="report-doc__section" aria-label={t("whatThisDid")}>
          <h2 className="report-doc__label">{t("whatThisDid")}</h2>
          <p className="report-doc__lead">{lead}</p>
        </section>
      )}

      {noteMessage && (
        <section className="report-doc__section" aria-label={t("note")}>
          <h2 className="report-doc__label">{t("note")}</h2>
          <p className="report-note">{noteMessage}</p>
        </section>
      )}

      <section className="report-doc__section" aria-label={t("howVerified")}>
        <h2 className="report-doc__label">{t("howVerified")}</h2>
        <VerifiedHow verifications={verifications} verified={verified} runId={deliverable.run_id} />
      </section>

      {showKnowledge && (
        <section className="report-doc__section" aria-label={t("knowledge")}>
          <h2 className="report-doc__label">{t("knowledge")}</h2>
          {references.length > 0 && (
            <div className="report-knowledge">
              <p className="report-knowledge__sublabel">{t("referenced")}</p>
              <p className="report-doc__muted">{t("referencedHint")}</p>
              <ul className="report-chips">
                {references.map((reference, i) => {
                  // A "Related note —" statement deep-links to the note viewer
                  // (R12); a canon-concept statement to the concept viewer (R13);
                  // a prior decision / rejection stays plain text. A long
                  // sentence-shaped reference renders as a readable block, not a
                  // stadium pill that mangles multi-line text (founder #1).
                  const statement = isStatementReference(reference)
                    ? " report-chip--statement"
                    : "";
                  const path = referenceNotePath(reference);
                  if (path) {
                    const label = prettyReference(reference);
                    return (
                      <li key={`ref-${i}-${reference}`}>
                        <button
                          type="button"
                          className={`report-chip report-chip--link${statement}`}
                          onClick={() => setOpenNote({ path, title: label })}
                        >
                          {label}
                        </button>
                      </li>
                    );
                  }
                  const conceptId = referenceConceptId(reference);
                  if (conceptId) {
                    return (
                      <li key={`ref-${i}-${reference}`}>
                        <button
                          type="button"
                          className={`report-chip report-chip--link${statement}`}
                          onClick={() => setOpenConcept({ id: conceptId, label: reference })}
                        >
                          {reference}
                        </button>
                      </li>
                    );
                  }
                  return (
                    <li key={`ref-${i}-${reference}`}>
                      <span className={`report-chip${statement}`}>{reference}</span>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
          {written.length > 0 && (
            <div className="report-knowledge">
              <p className="report-knowledge__sublabel">{t("written")}</p>
              <p className="report-doc__muted">{t("writtenHint")}</p>
              <ul className="report-chips">
                {written.map((note, i) => (
                  <li key={`written-${i}-${note.path}`}>
                    <button
                      type="button"
                      className="report-chip report-chip--written report-chip--link"
                      onClick={() => setOpenNote({ path: note.path, title: note.title })}
                    >
                      {note.title}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>
      )}

      {(fileCount > 0 || deliverable.artifact_uri) && (
        <details className="report-diff-disclosure">
          <summary className="report-diff-disclosure__summary">
            {t("seeFilesChanged", { count: fileCount })}
          </summary>
          {/* The diff/built panel is the document's SECONDARY surface (demoted)
              — kept in the DOM but collapsed by default behind the disclosure. */}
          <section className="report-diff-disclosure__body" aria-label={t("whatWasBuilt")}>
            <WhatWasBuilt deliverable={deliverable} hasDiff={hasDiff} />
          </section>
        </details>
      )}

      {/* The footer action MIRRORS the Brief for this item's state: a verified
          deliverable still HELD for approval ("Ready to ship") gets Approve &
          ship / Decline on its Safe-Mode item; only a SHIPPED run gets Rollback.
          Neither when there's nothing to act on. */}
      {report.held_delivery_item_id ? (
        <footer className="report-doc__footer">
          <ReportDeliveryActions itemId={report.held_delivery_item_id} onResolved={onResolved} />
        </footer>
      ) : report.run_status === "shipped" && canRollBack(deliverable) ? (
        <footer className="report-doc__footer">
          <RollbackAffordance deliverableId={deliverable.id} />
        </footer>
      ) : null}

      {openNote && (
        <NoteViewer
          path={openNote.path}
          fallbackTitle={openNote.title}
          onClose={() => setOpenNote(null)}
        />
      )}
      {openConcept && (
        <ConceptViewer
          conceptId={openConcept.id}
          fallbackLabel={openConcept.label}
          onClose={() => setOpenConcept(null)}
          // Click a related concept → navigate the same modal to it.
          onOpenConcept={(id, label) => setOpenConcept({ id, label })}
          // Click an observation → switch to the note viewer for that note.
          onOpenNote={(path, title) => {
            setOpenConcept(null);
            setOpenNote({ path, title });
          }}
        />
      )}
    </article>
  );
}

/** R13 — the concept viewer: a modal that fetches ONE canon concept's detail
 *  (its aliases, the related concepts it co-occurs with, and the observations
 *  that mention it) so a "참고한 지식" CONCEPT chip is verifiable in place — the
 *  hub-capped graph is hard to navigate to a single concept. Reuses the note
 *  viewer's <dialog> shell. A failed read shows a calm line, never a crash. */
function ConceptViewer({
  conceptId,
  fallbackLabel,
  onClose,
  onOpenConcept,
  onOpenNote,
}: {
  conceptId: string;
  fallbackLabel: string;
  onClose: () => void;
  onOpenConcept: (id: string, label: string) => void;
  onOpenNote: (path: string, title: string) => void;
}) {
  const t = useTranslations("report");
  const ref = useRef<HTMLDialogElement>(null);
  const [state, setState] = useState<
    { phase: "loading" } | { phase: "ready"; concept: ConceptDetail } | { phase: "error" }
  >({ phase: "loading" });

  useEffect(() => {
    const dialog = ref.current;
    if (dialog && !dialog.open) {
      try {
        dialog.showModal();
      } catch {
        dialog.open = true;
      }
    }
  }, []);

  useEffect(() => {
    let active = true;
    getConceptDetail(conceptId)
      .then((concept) => active && setState({ phase: "ready", concept }))
      .catch(() => active && setState({ phase: "error" }));
    return () => {
      active = false;
    };
  }, [conceptId]);

  const title = state.phase === "ready" ? state.concept.name : fallbackLabel;

  return (
    <dialog ref={ref} className="note-viewer" aria-label={title} onClose={onClose}>
      <div className="note-viewer__panel">
        <header className="note-viewer__head">
          <h2 className="note-viewer__title">{title}</h2>
          <button type="button" className="note-viewer__close" onClick={onClose}>
            {t("noteClose")}
          </button>
        </header>
        {/* Only the BODY scrolls — the head (title + Close) stays pinned so the
            founder can always dismiss the modal without scrolling back up. */}
        <div className="note-viewer__scroll">
          {state.phase === "loading" && (
            <p className="note-viewer__muted" aria-busy="true">
              {t("noteLoading")}
            </p>
          )}
          {state.phase === "error" && <p className="note-viewer__muted">{t("conceptError")}</p>}
          {state.phase === "ready" && (
            <div className="concept-viewer">
              {/* "What this concept is": its kind (Pattern / Principle / …) and any
                  alternative names. The concept notes carry no prose definition,
                  so the type + the linked example notes below ARE the definition. */}
              <p className="concept-viewer__lead">
                {state.concept.type ? (
                  <span className="concept-viewer__type">{state.concept.type}</span>
                ) : (
                  <span className="concept-viewer__type concept-viewer__type--plain">
                    {t("conceptKind")}
                  </span>
                )}
                {state.concept.aliases.length > 0 && (
                  <span className="concept-viewer__aliases">
                    {t("conceptAliases", { names: state.concept.aliases.join(", ") })}
                  </span>
                )}
              </p>
              {state.concept.related.length > 0 && (
                <section className="concept-viewer__group">
                  <p className="report-knowledge__sublabel">{t("conceptRelated")}</p>
                  <ul className="report-chips">
                    {state.concept.related.map((r) => (
                      <li key={r.id}>
                        <button
                          type="button"
                          className="report-chip report-chip--link"
                          onClick={() => onOpenConcept(r.id, r.name)}
                        >
                          {r.name}
                        </button>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
              {state.concept.observations.length > 0 && (
                <section className="concept-viewer__group">
                  <p className="report-knowledge__sublabel">{t("conceptObservations")}</p>
                  <ul className="concept-viewer__obs">
                    {state.concept.observations.map((o) => (
                      <li key={o.id}>
                        <button
                          type="button"
                          className="concept-viewer__obs-item concept-viewer__obs-item--link"
                          onClick={() => onOpenNote(o.id, o.title)}
                        >
                          {o.title}
                        </button>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
              {state.concept.related.length === 0 && state.concept.observations.length === 0 && (
                <p className="note-viewer__muted">{t("conceptEmpty")}</p>
              )}
            </div>
          )}
        </div>
      </div>
    </dialog>
  );
}

/** R12 — the note viewer: a modal that fetches ONE vault note's content (the
 *  note a run wrote or consulted) and renders it as Markdown, so a chip click
 *  shows the founder the ACTUAL note — the hub-capped graph drops fresh notes,
 *  so this is how a just-written note is verifiable. A native <dialog> gives the
 *  backdrop + Escape-to-close; a failed read shows a calm line, never a crash. */
function NoteViewer({
  path,
  fallbackTitle,
  onClose,
}: {
  path: string;
  fallbackTitle: string;
  onClose: () => void;
}) {
  const t = useTranslations("report");
  const ref = useRef<HTMLDialogElement>(null);
  const [state, setState] = useState<
    { phase: "loading" } | { phase: "ready"; note: KnowledgeNote } | { phase: "error" }
  >({ phase: "loading" });

  useEffect(() => {
    const dialog = ref.current;
    if (dialog && !dialog.open) {
      try {
        dialog.showModal();
      } catch {
        dialog.open = true;
      }
    }
  }, []);

  useEffect(() => {
    let active = true;
    getNote(path)
      .then((note) => active && setState({ phase: "ready", note }))
      .catch(() => active && setState({ phase: "error" }));
    return () => {
      active = false;
    };
  }, [path]);

  const title = state.phase === "ready" ? state.note.title : fallbackTitle;

  return (
    <dialog ref={ref} className="note-viewer" aria-label={title} onClose={onClose}>
      <div className="note-viewer__panel">
        <header className="note-viewer__head">
          <h2 className="note-viewer__title">{title}</h2>
          <button type="button" className="note-viewer__close" onClick={onClose}>
            {t("noteClose")}
          </button>
        </header>
        {/* Only the BODY scrolls — the head (title + Close) stays pinned so the
            founder can always dismiss the modal without scrolling back up. */}
        <div className="note-viewer__scroll">
          {state.phase === "loading" && (
            <p className="note-viewer__muted" aria-busy="true">
              {t("noteLoading")}
            </p>
          )}
          {state.phase === "error" && <p className="note-viewer__muted">{t("noteError")}</p>}
          {state.phase === "ready" && (
            <div className="note-viewer__body report-markdown">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{state.note.content}</ReactMarkdown>
            </div>
          )}
        </div>
      </div>
    </dialog>
  );
}

/** R8 — the held-delivery footer action, the SAME decision as the Brief's
 *  "Ready to ship" card: Approve & ship dispatches the held delivery
 *  (`POST /api/v1/safemode/{id}/approve`), Decline drops it (`…/deny`). Resolves
 *  inline; on success the parent re-reads the report so the footer reflects the
 *  new state. A failed call keeps the buttons actionable with a calm message. */
function ReportDeliveryActions({
  itemId,
  onResolved,
}: {
  itemId: string;
  onResolved: () => void;
}) {
  // Use the SAME (decisions) namespace as the Brief's DeliveryRow so the report
  // footer buttons read IDENTICALLY to the Brief card (founder: "보고서 버튼과
  // 요약탭 버튼 이름은 동일해야 해") — Approve / Decline, not "Approve & ship".
  const t = useTranslations("decisions");
  const [state, setState] = useState<"idle" | "working" | "error">("idle");
  const working = state === "working";

  const run = async (action: "approve" | "deny") => {
    if (working) return;
    setState("working");
    try {
      if (action === "approve") {
        await approveSafeModeItem(itemId);
      } else {
        await denySafeModeItem(itemId);
      }
      onResolved();
    } catch {
      setState("error");
    }
  };

  return (
    <div className="report-actions">
      <button
        type="button"
        className="need-card__btn need-card__btn--primary"
        onClick={() => run("approve")}
        disabled={working}
      >
        {working ? t("working") : t("approve")}
      </button>
      <button
        type="button"
        className="need-card__btn need-card__btn--secondary"
        onClick={() => run("deny")}
        disabled={working}
      >
        {t("decline")}
      </button>
      {state === "error" && (
        <span className="need-card__error" aria-live="polite">
          {t("resolveError")}
        </span>
      )}
    </div>
  );
}

/** The OPTIONAL "Note" message — surfaced ONLY when the strongest verification
 *  outcome is NOT `passed` AND it carries a human-readable message in its
 *  result. A clean pass (or a non-passed outcome with no message) returns null,
 *  so the section is omitted entirely — never a fabricated note. */
function nonPassedNote(verifications: VerificationReportItem[]): string | null {
  const strongest = strongestOutcome(verifications);
  if (strongest === null || strongest === "passed") return null;
  // Find a verification carrying the strongest outcome and a result message.
  for (const v of verifications) {
    if (v.outcome !== strongest) continue;
    const message = summarizeResult(v.result);
    if (message) return message;
  }
  return null;
}

/** Whether a Rollback affordance makes sense for this deliverable. A pure
 *  DIRECT_OUTPUT answer produced no external artifact (no PR / message / page)
 *  to reverse, so the backend would 400 `no_compensation_handle` — hide it
 *  rather than offer a button that can only say "nothing to roll back". Every
 *  other type CAN carry a compensation handle; if it happens not to, the backend
 *  400 still degrades to the calm "nothing to roll back" state. */
function canRollBack(deliverable: DeliverableReport["deliverable"]): boolean {
  return deliverable.deliverable_type !== "direct_output";
}

/** Rollback / 되돌리기 — the read-only report's ONE mutating affordance. A button
 *  opens an inline confirm (no modal dependency — the report is a document, not a
 *  graph); confirming POSTs `retract` and settles into a calm terminal state.
 *
 *  Response variants are mapped to calm copy, never an error wall:
 *   - 200 first retract → "Rolled back" (+ what was reverted, when the backend
 *     names it, e.g. "PR closed").
 *   - 200 already_retracted → calm "Already rolled back".
 *   - 400 no_compensation_handle → calm "Nothing to roll back" (no external
 *     artifact existed).
 *   - 502 / other → calm "Couldn't roll back — try again" (the button returns so
 *     the founder can retry; the backend left the row un-retracted).
 */
function RollbackAffordance({ deliverableId }: { deliverableId: string }) {
  const t = useTranslations("report");
  // idle → confirming → pending → done (terminal). A recoverable failure (502 /
  // network) returns to `confirming` carrying `error` so the founder retries in
  // one click without losing the confirm context.
  type Phase =
    | { kind: "idle" }
    | { kind: "confirming"; error: string | null }
    | { kind: "pending" }
    | { kind: "done"; message: string };
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });

  const confirm = async () => {
    setPhase({ kind: "pending" });
    try {
      const result = await retractDeliverable(deliverableId);
      if (result.already_retracted) {
        setPhase({ kind: "done", message: t("rollbackAlready") });
        return;
      }
      // Name what was reverted when the backend reports a compensated entry
      // (e.g. "pr" / "github"); otherwise the plain "Rolled back".
      const what = result.compensated
        .map((entry) => entry.artifact_type || entry.plugin)
        .filter(Boolean)
        .join(", ");
      setPhase({
        kind: "done",
        message: what ? t("rollbackDoneWith", { what }) : t("rollbackDone"),
      });
    } catch (error: unknown) {
      // 400 no_compensation_handle → nothing to revert (calm terminal). Any other
      // failure (502 compensate_failed, network) → back to the confirm step with
      // a calm "couldn't roll back, try again" so a retry is one click away.
      if (error instanceof ApiError && error.status === 400) {
        setPhase({ kind: "done", message: t("rollbackNothing") });
      } else {
        setPhase({ kind: "confirming", error: t("rollbackFailed") });
      }
    }
  };

  if (phase.kind === "done") {
    return (
      <p className="report-rollback__done" aria-live="polite">
        {phase.message}
      </p>
    );
  }

  return (
    <div className="report-rollback">
      {phase.kind === "idle" ? (
        <button
          type="button"
          className="report-rollback__open"
          onClick={() => setPhase({ kind: "confirming", error: null })}
        >
          {t("rollback")}
        </button>
      ) : (
        <div className="report-rollback__confirm">
          <p className="report-rollback__lede">{t("rollbackConfirmLede")}</p>
          {phase.kind === "confirming" && phase.error && (
            <p className="report-rollback__error" aria-live="polite">
              {phase.error}
            </p>
          )}
          <div className="report-rollback__actions">
            <button
              type="button"
              className="report-rollback__cancel"
              onClick={() => setPhase({ kind: "idle" })}
              disabled={phase.kind === "pending"}
            >
              {t("rollbackCancel")}
            </button>
            <button
              type="button"
              className="report-rollback__danger"
              onClick={confirm}
              disabled={phase.kind === "pending"}
            >
              {phase.kind === "pending" ? t("rollbackPending") : t("rollbackConfirmButton")}
            </button>
          </div>
        </div>
      )}
    </div>
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

// The retrieved-knowledge judge checks (rationale below) are NOT verification —
// they're the canon/prior-decision statements the retriever folded into the
// contract, which the backend ALSO extracts into the separate `references`
// array (surfaced as "What BSVibe referenced"). Surfacing them again in the
// "How BSVibe checked this" list double-counts knowledge as a check. Mirror the
// backend's RETRIEVED_KNOWLEDGE_RATIONALE / LEGACY_… constants
// (backend/workflow/application/verification_service.py) to filter them out so
// the verification list shows ONLY real checks (ruff/format/mypy/pytest commands
// + genuine acceptance-judge criteria). Knowledge is reference, not verification.
const _RETRIEVED_KNOWLEDGE_RATIONALES: ReadonlySet<string> = new Set([
  "Canonical patterns retrieved for this change",
  "BSage canonical patterns retrieved for this change",
]);

/** "How it was verified" — the ONE authoritative result read as proof, with the
 *  superseded retry attempts collapsed behind a quiet disclosure so a run that
 *  retried before passing isn't a wall of red (founder #2). When nothing was
 *  verified, a calm line. */
function VerifiedHow({
  verifications,
  verified,
  runId,
}: {
  verifications: VerificationReportItem[];
  verified: boolean;
  runId: string;
}) {
  const t = useTranslations("report");
  const { authoritative, earlier } = splitVerifications(verifications, verified);
  if (authoritative === null) {
    return <p className="report-doc__muted">{t("noVerification")}</p>;
  }
  return (
    <>
      <AuthoritativeVerification verification={authoritative} runId={runId} />
      {earlier.length > 0 && (
        <details className="report-attempts">
          <summary className="report-attempts__summary">
            {t("earlierAttempts", { count: earlier.length })}
          </summary>
          <div className="report-attempts__body">
            {earlier.map((v) => (
              <VerificationBlock key={v.id} verification={v} />
            ))}
          </div>
        </details>
      )}
    </>
  );
}

/** The authoritative verification, rendered as the founder-facing proof: the
 *  honesty grade (how strongly a pass holds), the declared checks, the I2
 *  result-demonstration ("we ran it and saw this"), and — when it did NOT pass —
 *  WHY (the failing command + its real output) plus a next step (open the run to
 *  retry). */
function AuthoritativeVerification({
  verification,
  runId,
}: {
  verification: VerificationReportItem;
  runId: string;
}) {
  const t = useTranslations("report");
  const passed = verification.outcome === "passed";
  const failed = verification.outcome === "failed";
  const grade = passed ? honestyGrade(verification) : null;
  const demo = demonstration(verification.result);
  // The AUTHORITATIVE gate is the repo's OWN derived checks; the agent's declared
  // commands are advisory. Lead with the derived gate when present; an older row
  // (no derived gate) falls back to the declared-contract checklist.
  const gate = derivedGate(verification.result);
  const hasGate = gate !== null && gate.commands.length > 0;
  // On a failure, the failing checks come from the authoritative gate when we
  // have it (a failed derived command), else the run's command_results.
  const failingRuns = !failed
    ? []
    : hasGate
      ? gate.commands
          .filter((c) => c.status === "failed")
          .map((c) => ({ command: c.command, passed: false, exitCode: null, output: "" }))
      : commandRuns(verification.result).filter((c) => !c.passed);

  return (
    <div className="report-verify">
      {grade && (
        <span
          className={`report-grade report-grade--${grade.toLowerCase()}`}
          title={t(`gradeHint.${grade}`)}
        >
          {t("gradeLabel", { grade })}
        </span>
      )}
      {hasGate ? (
        <DerivedGateChecklist gate={gate} />
      ) : (
        <VerificationBlock verification={verification} />
      )}
      {demo && <DemonstrationLine demo={demo} />}
      {failed && (
        <div className="report-verify-fail">
          <p className="report-verify-fail__lead">{t("verifyFailedLead")}</p>
          {failingRuns.length > 0 && (
            <ul className="report-verify-fail__cmds">
              {failingRuns.map((c, i) => {
                const tail = tailOutput(c.output);
                return (
                  <li key={`${c.command}-${i}`} className="report-verify-fail__cmd">
                    <div className="report-verify-fail__cmd-head">
                      <code className="report-verify-fail__cmd-text">{c.command}</code>
                      {c.exitCode !== null && (
                        <span className="report-verify-fail__exit">
                          {t("exitCode", { code: c.exitCode })}
                        </span>
                      )}
                    </div>
                    {tail && <pre className="report-verify-fail__out">{tail}</pre>}
                  </li>
                );
              })}
            </ul>
          )}
          <p className="report-verify-fail__next">
            {t("verifyFailedHint")}{" "}
            <Link href={`/runs/${runId}`} className="report-verify-fail__link">
              {t("openRunToRetry")}
            </Link>
          </p>
        </div>
      )}
    </div>
  );
}

/** The I2 result-demonstration line — "we ran it against the result and saw
 *  this". A `demonstrated` verdict lists the probes that matched; an
 *  `undemonstrable` / `failed` verdict reads as a calm honest line. */
function DemonstrationLine({ demo }: { demo: Demonstration }) {
  const t = useTranslations("report");
  if (demo.verdict === "demonstrated") {
    return (
      <div className="report-demo">
        <p className="report-demo__lead">{t("demoDemonstrated", { count: demo.probes.length })}</p>
        {demo.probes.length > 0 && (
          <ul className="report-demo__probes">
            {demo.probes.map((p, i) => (
              <li key={`${p.name}-${i}`} className="report-demo__probe">
                {p.name}
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  }
  if (demo.verdict === "undemonstrable") {
    return <p className="report-demo__muted">{t("demoUndemonstrable")}</p>;
  }
  if (demo.verdict === "failed") {
    return <p className="report-demo__muted">{t("demoFailed")}</p>;
  }
  return null;
}

/** The authoritative "How it was verified" list — the repo's OWN derived-gate
 *  commands, each with the status the verifier observed (passed / failed / a tool
 *  that wasn't available here). This is what actually gated the verdict; the
 *  agent's declared commands are advisory and not shown here. */
function DerivedGateChecklist({ gate }: { gate: DerivedGate }) {
  const t = useTranslations("report");
  return (
    <div className="report-gate">
      <p className="report-gate__label">{t("derivedChecksLabel")}</p>
      <ul className="report-checklist">
        {gate.commands.map((c, i) => {
          const tone = c.status === "passed" ? "passed" : c.status === "failed" ? "other" : "muted";
          const tagKey = `gateStatusTag.${c.status === "unavailable" ? "unavailable" : c.status === "failed" ? "failed" : "passed"}`;
          return (
            <li key={`gate-${i}-${c.command}`} className="report-checklist__row">
              <span
                className={`report-checklist__mark report-checklist__mark--${tone}`}
                aria-hidden="true"
              >
                {c.status === "passed" ? (
                  <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
                    <path
                      d="M13 4.5 6.5 11 3 7.5"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                ) : c.status === "unavailable" ? (
                  "○"
                ) : (
                  "•"
                )}
              </span>
              <span className="report-checklist__label">{c.command}</span>
              <span className={`report-checklist__tag report-checklist__tag--${tone}`}>
                {t(tagKey)}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function VerificationBlock({ verification }: { verification: VerificationReportItem }) {
  const t = useTranslations("report");
  // Drop the retrieved-knowledge checks — they belong only under "Knowledge",
  // never the verification list (they'd otherwise duplicate the `references`
  // group AND read as a check the change had to pass). Keep the L12 filter.
  const allChecks = checksFromContract(verification.contract);
  const checks = allChecks.filter((check) => !_RETRIEVED_KNOWLEDGE_RATIONALES.has(check.rationale));
  // Two distinct empty states: the contract genuinely declared no checks
  // (`noChecksDeclared`), vs. its only checks were retrieved-knowledge ones we
  // filtered out (`noChecksAfterKnowledge` — the knowledge shows under
  // "Knowledge"; don't claim "nothing was verified").
  const emptyKey = allChecks.length > 0 ? "noChecksAfterKnowledge" : "noChecksDeclared";
  // The verdict tag each row carries — the verification's outcome rendered as a
  // small plain tag (passed / failed / inconclusive), not the raw shell exit.
  const tagKey = `outcomeTag.${verification.outcome}` as const;
  const passed = verification.outcome === "passed";
  if (checks.length === 0) {
    return <p className="report-checks__none">{t(emptyKey)}</p>;
  }
  return (
    <ul className="report-checklist">
      {checks.map((check, i) => (
        <li
          // Checks are positional, free-form, and may repeat — index keys the row.
          key={`${verification.id}-${i}`}
          className="report-checklist__row"
        >
          <span
            className={`report-checklist__mark report-checklist__mark--${passed ? "passed" : "other"}`}
            aria-hidden="true"
          >
            {passed ? (
              <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
                <path
                  d="M13 4.5 6.5 11 3 7.5"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            ) : (
              "•"
            )}
          </span>
          <span className="report-checklist__label">{check.label}</span>
          <span
            className={`report-checklist__tag report-checklist__tag--${passed ? "passed" : "other"}`}
          >
            {t(tagKey)}
          </span>
        </li>
      ))}
    </ul>
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
  // Keep the currently DISPLAYED file as a consistent (name, content) pair and
  // hold it on screen while the NEXT file's content is fetched — so switching a
  // file never blanks to a loading note (and never reflows). The left file
  // list's active marker moves instantly, signalling the load; we swap the panel
  // only when the new content arrives. The very first load (nothing to keep yet)
  // shows the loading note; a failed read surfaces the calm note rather than
  // leaving a stale file under a new selection.
  const [shown, setShown] = useState<{ fileName: string; artifact: ArtifactContent } | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    setFailed(false);
    getDeliverableArtifact(deliverableId, fileName)
      .then((artifact) => {
        if (active) setShown({ fileName, artifact });
      })
      .catch(() => {
        // 404 (file cleaned / not whitelisted) OR any read failure → the calm
        // "couldn't show this file" fallback (never an error wall).
        if (active) setFailed(true);
      });
    return () => {
      active = false;
    };
  }, [deliverableId, fileName]);

  if (failed) {
    return (
      <p className="report-artifact-view__unavailable">
        {hasDiff ? t("artifactUnavailableWithDiff") : t("artifactUnavailable")}
      </p>
    );
  }

  if (shown === null) {
    return (
      <p className="report-artifact-view__loading" aria-busy="true">
        {t("artifactLoading")}
      </p>
    );
  }

  const { artifact } = shown;
  if (artifact.binary) {
    return <p className="report-artifact-view__binary">{artifact.content}</p>;
  }
  // A freshly produced Markdown doc reads best RENDERED (headings, lists, bold)
  // — a code-diff with `+` gutters is a developer metaphor that confuses a
  // non-developer. An EDITED Markdown file still shows the diff (handled by the
  // caller's captured-diff branch); only a no-before doc reaches here.
  const isMarkdown = langFromFileName(shown.fileName) === "markdown";
  return (
    <>
      {artifact.truncated && (
        <p className="report-artifact-view__truncated">{t("artifactTruncated")}</p>
      )}
      {isMarkdown ? (
        <MarkdownDoc content={artifact.content} />
      ) : (
        <HighlightedDiff
          fileName={shown.fileName}
          hunk={synthesizeAdditionHunk(shown.fileName, artifact.content)}
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
