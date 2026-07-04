"use client";

import { resolveCheckpoint, resolveCheckpointAction } from "@/lib/api/checkpoints";
import type { CheckpointAction, PendingCheckpoint } from "@/lib/api/types";
import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { useId, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type RowState = "idle" | "working" | "error";

/**
 * One paused-run checkpoint as a CARD in the unified Brief "Needs you" (R7). The
 * agent is blocked on a question (`item.question`); the founder's resolution
 * resumes the run via POST /api/v1/checkpoints/{id}/resolve.
 *
 * The card leads with the task (`item.title`) + an amber "Needs your answer"
 * status, then the question as the body. Two interaction shapes:
 *
 * 1. **Options + free-text (the AskUserQuestion shape).** When the work LLM
 *    offered `options`, they render as selectable CHIPS plus a persistent
 *    "Or type your own answer…" input — pick a chip OR type a custom reply.
 *    A typed answer takes precedence over a selected chip. Pure free-text (no
 *    options) is the same input on its own.
 *
 * 2. **One-click actions (executor B2b Decisions).** For
 *    `verification_failed` / `human_review_required`, the backend ships
 *    `actions` like `[{key:"ship",...}, {key:"discard",...}]`, rendered as
 *    buttons that POST `{ action_key }`. An "Other (free-text)" disclosure
 *    stays available so the founder can always explain a custom path.
 */
export default function CheckpointRow({
  item,
  onResolved,
}: {
  item: PendingCheckpoint;
  onResolved: () => void;
}) {
  const t = useTranslations("decisions");
  const locale = useLocale();
  const offered: string[] | null = item.options && item.options.length > 0 ? item.options : null;
  const hasOptions = offered !== null;
  const actions: CheckpointAction[] | null =
    item.actions && item.actions.length > 0 ? item.actions : null;
  const hasActions = actions !== null;

  // `selected` is the chosen option chip (options mode); `answer` is the
  // free-text input. A typed answer takes precedence over a selected chip.
  // `freeTextOpen` toggles the optional free-text reply in actions mode.
  const [selected, setSelected] = useState<string | null>(null);
  const [answer, setAnswer] = useState("");
  const [freeTextOpen, setFreeTextOpen] = useState(false);
  const [state, setState] = useState<RowState>("idle");

  const answerId = useId();
  const actionFreeTextId = useId();

  const answerTrimmed = answer.trim();
  const working = state === "working";

  const labelFor = (a: CheckpointAction): string => (locale === "ko" ? a.label_ko : a.label_en);

  // The resolution payload (used outside the one-click action path): a typed
  // answer wins; otherwise the selected chip (options mode); else nothing.
  const freeTextPayload = answerTrimmed || (hasOptions ? (selected ?? "") : "");
  const freeTextSubmitDisabled = working || freeTextPayload.length === 0;

  async function submitFreeText() {
    if (freeTextSubmitDisabled) return;
    setState("working");
    try {
      await resolveCheckpoint(item.checkpointId, freeTextPayload);
      onResolved();
    } catch {
      setState("error");
    }
  }

  async function submitAction(actionKey: string) {
    if (working) return;
    setState("working");
    try {
      await resolveCheckpointAction(item.checkpointId, actionKey);
      onResolved();
    } catch {
      setState("error");
    }
  }

  return (
    <li className="need-card need-card--decision">
      <div className="need-card__head">
        {/* Lead with the task the founder is judging, not a bare question. */}
        <div className="need-card__title-wrap">
          <span className="need-card__title">{item.title || item.question}</span>
          {item.productSlug && item.productSlug !== "workspace" && (
            <span className="need-card__product">{item.productSlug}</span>
          )}
        </div>
        <span className="need-card__status">
          <span className="need-card__status-dot" aria-hidden="true" />
          {t("needsYourAnswer")}
        </span>
      </div>

      {/* The ask + rationale are agent-authored prose the founder reads to
          decide — render markdown (lists of options, `code` refs, emphasis)
          rather than dumping raw syntax. */}
      {item.title ? (
        <div className="need-card__body markdown-inline">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{item.question}</ReactMarkdown>
        </div>
      ) : null}
      {item.rationale ? (
        <div className="need-card__rationale markdown-inline">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{item.rationale}</ReactMarkdown>
        </div>
      ) : null}

      {item.priorDecisions.length > 0 ? (
        <div className="need-card__prior" aria-label={t("priorDecisions")}>
          <span className="need-card__prior-label">{t("priorDecisions")}</span>
          <ul className="need-card__prior-list">
            {item.priorDecisions.map((prior, i) => (
              // Prior statements are free-form and may repeat across re-asks
              // (deduped server-side); index keys the row.
              <li key={`${i}-${prior}`}>{prior}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {hasActions && actions !== null ? (
        <>
          <div className="need-card__actions">
            {actions.map((a, i) => (
              <button
                key={a.key}
                type="button"
                className={`need-card__btn ${i === 0 ? "need-card__btn--primary" : "need-card__btn--secondary"}`}
                onClick={() => submitAction(a.key)}
                disabled={working}
              >
                {labelFor(a)}
              </button>
            ))}
            {state === "error" && (
              <span className="need-card__error" aria-live="polite">
                {t("resolveError")}
              </span>
            )}
            {item.detailHref && (
              <>
                <span className="need-card__spacer" />
                <Link className="need-card__view" href={item.detailHref}>
                  {t("viewProof")}
                </Link>
              </>
            )}
          </div>
          {/* Optional free-text reply alongside the one-click actions. */}
          {!freeTextOpen ? (
            <button
              type="button"
              className="need-card__free-toggle"
              onClick={() => setFreeTextOpen(true)}
              disabled={working}
            >
              {t("otherOptionLabel")}
            </button>
          ) : (
            <div className="need-card__answer-row">
              <input
                id={actionFreeTextId}
                className="need-card__input"
                placeholder={t("otherAnswerPlaceholder")}
                value={answer}
                disabled={working}
                aria-label={t("answerLabel")}
                onChange={(e) => setAnswer(e.target.value)}
              />
              <button
                type="button"
                className="need-card__btn need-card__btn--primary"
                onClick={submitFreeText}
                disabled={freeTextSubmitDisabled}
              >
                {working ? t("working") : t("answer")}
              </button>
            </div>
          )}
        </>
      ) : (
        <>
          {hasOptions && offered !== null ? (
            <fieldset className="need-card__options" aria-label={t("answerLabel")}>
              {offered.map((opt) => (
                <button
                  key={opt}
                  type="button"
                  className={`need-card__opt${selected === opt ? " need-card__opt--on" : ""}`}
                  aria-pressed={selected === opt}
                  disabled={working}
                  onClick={() => setSelected(selected === opt ? null : opt)}
                >
                  {opt}
                </button>
              ))}
            </fieldset>
          ) : null}
          <div className="need-card__answer-row">
            <input
              id={answerId}
              className="need-card__input"
              placeholder={hasOptions ? t("orTypeYourOwn") : t("answerPlaceholder")}
              value={answer}
              disabled={working}
              aria-label={t("answerLabel")}
              onChange={(e) => setAnswer(e.target.value)}
            />
            <button
              type="button"
              className="need-card__btn need-card__btn--primary"
              onClick={submitFreeText}
              disabled={freeTextSubmitDisabled}
            >
              {working ? t("working") : t("answer")}
            </button>
          </div>
          {state === "error" ? (
            <span className="need-card__error" aria-live="polite">
              {t("resolveError")}
            </span>
          ) : null}
        </>
      )}
    </li>
  );
}
