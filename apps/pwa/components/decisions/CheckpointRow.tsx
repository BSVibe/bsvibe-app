"use client";

import { resolveCheckpoint, resolveCheckpointAction } from "@/lib/api/checkpoints";
import type { CheckpointAction, PendingCheckpoint } from "@/lib/api/types";
import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { useId, useState } from "react";
import { relativeTime } from "./relative-time";

type RowState = "idle" | "working" | "error";

// Sentinel for the "Other → free-text" radio. Picked so it can never collide
// with a real LLM-supplied option string.
const OTHER_SENTINEL = "__bsvibe_other__";

/**
 * One paused-run checkpoint row in the unified Pending list. The agent is
 * blocked on a question (`item.question`); the founder's resolution resumes
 * the run via POST /api/v1/checkpoints/{id}/resolve.
 *
 * Two interaction shapes:
 *
 * 1. **Free-text / suggestions (L-D1).** When the work LLM offered
 *    `options`, render them as radios PLUS an "Other" radio that reveals
 *    a free-text textarea. The backend records whichever string was
 *    submitted verbatim. Pure free-text (no options) → plain textarea.
 *
 * 2. **One-click actions (L-D2).** For executor B2b Decisions
 *    (`verification_failed`, `human_review_required`), the backend ships
 *    `actions` like `[{key:"ship",label_en:"Approve & ship",...}, ...]`.
 *    Render those as dedicated buttons that POST `{ action_key }` and
 *    trigger side effects (ship → deliverable + shipped run; discard →
 *    cancelled run). A small "Other (free-text)" disclosure stays
 *    available below so the founder can always type a free reply.
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

  // In options mode the radio drives `selected`; "Other" reveals `otherText`.
  // In free-text mode only `answer` is used. In actions mode `freeTextOpen`
  // toggles the disclosure for an optional free-text reply alongside the
  // action buttons (the founder may explain a discard or a custom path).
  const [selected, setSelected] = useState<string | null>(null);
  const [otherText, setOtherText] = useState("");
  const [answer, setAnswer] = useState("");
  const [freeTextOpen, setFreeTextOpen] = useState(false);
  const [state, setState] = useState<RowState>("idle");

  const answerId = useId();
  const otherId = useId();
  const actionFreeTextId = useId();
  const optionsGroupName = useId();

  const otherActive = hasOptions && selected === OTHER_SENTINEL;
  const otherTrimmed = otherText.trim();
  const freeTrimmed = answer.trim();
  const working = state === "working";

  const labelFor = (a: CheckpointAction): string => (locale === "ko" ? a.label_ko : a.label_en);

  // Free-text payload (used outside of the action-button path).
  const freeTextPayload = (() => {
    if (hasActions) return freeTrimmed;
    if (!hasOptions) return freeTrimmed;
    if (selected === OTHER_SENTINEL) return otherTrimmed;
    return selected ?? "";
  })();
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
    <li className="decisions-row decisions-row--decision">
      <span className="decisions-row__main">
        <span className="decisions-row__q">{item.question}</span>
        {/* The task this checkpoint belongs to, so the founder knows which work
            they're judging instead of answering a bare question. */}
        {item.title && <span className="decisions-row__sub">{item.title}</span>}
        <span className="decisions-row__meta">
          <span className="decisions-chip decisions-chip--decision">{t("kindDecision")}</span>
          {item.productSlug && item.productSlug !== "workspace" && (
            <span className="decisions-row__product">{item.productSlug}</span>
          )}
          {item.detailHref && (
            <Link className="decisions-row__view" href={item.detailHref}>
              {t("viewProof")}
            </Link>
          )}
          <span className="decisions-row__time">{relativeTime(item.createdAt, t)}</span>
        </span>
      </span>
      {item.rationale ? <p className="decisions-row__rationale">{item.rationale}</p> : null}

      {item.priorDecisions.length > 0 ? (
        <div className="decisions-row__prior" aria-label={t("priorDecisions")}>
          <span className="decisions-row__prior-label">{t("priorDecisions")}</span>
          <ul className="decisions-row__prior-list">
            {item.priorDecisions.map((prior, i) => (
              // Prior statements are free-form and may repeat across re-asks
              // (deduped server-side); index keys the row.
              <li key={`${i}-${prior}`} className="decisions-row__prior-item">
                {prior}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {hasActions && actions !== null ? (
        <>
          <div className="decisions-row__action-buttons">
            {actions.map((a) => (
              <button
                key={a.key}
                type="button"
                className={`decisions-row__action decisions-row__action--${a.key}`}
                onClick={() => submitAction(a.key)}
                disabled={working}
              >
                {labelFor(a)}
              </button>
            ))}
          </div>
          {/* Optional free-text reply alongside the one-click actions. */}
          {!freeTextOpen ? (
            <button
              type="button"
              className="decisions-row__free-text-toggle"
              onClick={() => setFreeTextOpen(true)}
              disabled={working}
            >
              {t("otherOptionLabel")}
            </button>
          ) : (
            <>
              <label className="decisions-row__answer-label" htmlFor={actionFreeTextId}>
                {t("answerLabel")}
              </label>
              <textarea
                id={actionFreeTextId}
                className="decisions-row__answer"
                rows={2}
                placeholder={t("otherAnswerPlaceholder")}
                value={answer}
                disabled={working}
                onChange={(e) => setAnswer(e.target.value)}
              />
              <button
                type="button"
                className="decisions-row__primary"
                onClick={submitFreeText}
                disabled={freeTextSubmitDisabled}
              >
                {working ? t("working") : t("answer")}
              </button>
            </>
          )}
        </>
      ) : hasOptions && offered !== null ? (
        <>
          <fieldset className="decisions-row__options" aria-label={t("answerLabel")}>
            {offered.map((opt) => {
              const id = `${optionsGroupName}-${opt}`;
              return (
                <label key={opt} className="decisions-row__option" htmlFor={id}>
                  <input
                    id={id}
                    type="radio"
                    name={optionsGroupName}
                    value={opt}
                    checked={selected === opt}
                    disabled={working}
                    onChange={() => setSelected(opt)}
                  />
                  <span>{opt}</span>
                </label>
              );
            })}
            <label
              key={OTHER_SENTINEL}
              className="decisions-row__option"
              htmlFor={`${optionsGroupName}-other`}
            >
              <input
                id={`${optionsGroupName}-other`}
                type="radio"
                name={optionsGroupName}
                value={OTHER_SENTINEL}
                checked={otherActive}
                disabled={working}
                onChange={() => setSelected(OTHER_SENTINEL)}
              />
              <span>{t("otherOptionLabel")}</span>
            </label>
            {otherActive ? (
              <textarea
                id={otherId}
                className="decisions-row__answer"
                rows={2}
                placeholder={t("otherAnswerPlaceholder")}
                value={otherText}
                disabled={working}
                aria-label={t("otherOptionLabel")}
                onChange={(e) => setOtherText(e.target.value)}
              />
            ) : null}
          </fieldset>
          <span className="decisions-row__actions">
            {state === "error" && (
              <span className="decisions-row__error" aria-live="polite">
                {t("resolveError")}
              </span>
            )}
            <button
              type="button"
              className="decisions-row__primary"
              onClick={submitFreeText}
              disabled={freeTextSubmitDisabled}
            >
              {working ? t("working") : t("answer")}
            </button>
          </span>
        </>
      ) : (
        <>
          <label className="decisions-row__answer-label" htmlFor={answerId}>
            {t("answerLabel")}
          </label>
          <textarea
            id={answerId}
            className="decisions-row__answer"
            rows={2}
            placeholder={t("answerPlaceholder")}
            value={answer}
            disabled={working}
            onChange={(e) => setAnswer(e.target.value)}
          />
          <span className="decisions-row__actions">
            {state === "error" && (
              <span className="decisions-row__error" aria-live="polite">
                {t("resolveError")}
              </span>
            )}
            <button
              type="button"
              className="decisions-row__primary"
              onClick={submitFreeText}
              disabled={freeTextSubmitDisabled}
            >
              {working ? t("working") : t("answer")}
            </button>
          </span>
        </>
      )}

      {hasActions && state === "error" ? (
        <span className="decisions-row__error" aria-live="polite">
          {t("resolveError")}
        </span>
      ) : null}
    </li>
  );
}
