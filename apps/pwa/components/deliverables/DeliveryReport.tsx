"use client";

import { relativeTime } from "@/components/decisions/relative-time";
import { ApiError } from "@/lib/api/client";
import {
  getDeliverableArtifact,
  getDeliverableDiff,
  getDeliverableReport,
  retractDeliverable,
} from "@/lib/api/deliverables";
import { approveSafeModeItem, denySafeModeItem } from "@/lib/api/safemode";
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
        {verifications.length === 0 ? (
          <p className="report-doc__muted">{t("noVerification")}</p>
        ) : (
          verifications.map((v) => <VerificationBlock key={v.id} verification={v} />)
        )}
      </section>

      {showKnowledge && (
        <section className="report-doc__section" aria-label={t("knowledge")}>
          <h2 className="report-doc__label">{t("knowledge")}</h2>
          {references.length > 0 && (
            <div className="report-knowledge">
              <p className="report-knowledge__sublabel">{t("referenced")}</p>
              <p className="report-doc__muted">{t("referencedHint")}</p>
              <ul className="report-chips">
                {references.map((reference, i) => (
                  // References are free-form statements that may repeat across
                  // re-attempts (deduped server-side); index keys the chip. A
                  // "Related note — <path>.md" statement is shown as a readable
                  // note title (note-level), not a raw internal path.
                  <li key={`ref-${i}-${reference}`} className="report-chip">
                    {prettyReference(reference)}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {written.length > 0 && (
            <div className="report-knowledge">
              <p className="report-knowledge__sublabel">{t("written")}</p>
              <p className="report-doc__muted">{t("writtenHint")}</p>
              <ul className="report-chips">
                {written.map((note, i) => (
                  <li key={`written-${i}-${note}`} className="report-chip report-chip--written">
                    {note}
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
    </article>
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
