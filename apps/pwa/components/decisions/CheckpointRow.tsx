"use client";

import { resolveCheckpoint } from "@/lib/api/checkpoints";
import type { PendingCheckpoint } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";
import { relativeTime } from "./relative-time";

type RowState = "idle" | "working" | "error";

// Sentinel for the "Other → free-text" radio. Picked so it can never collide
// with a real LLM-supplied option string.
const OTHER_SENTINEL = "__bsvibe_other__";

/**
 * One paused-run checkpoint row in the unified Pending list. The agent is
 * blocked on a question (`item.question`); the founder's answer resumes the
 * run via POST /api/v1/checkpoints/{id}/resolve ({ answer }, non-empty — the
 * backend ResolveRequest requires min_length=1, so the submit stays disabled
 * until there is real text).
 *
 * L-D1 — when the work LLM offered concrete choices (`item.options` non-
 * empty), render them as a single-select PLUS an "Other" radio that reveals
 * a free-text textarea. The options are *suggestions*: the founder may pick
 * one of them OR write their own answer. The backend records whichever
 * string was submitted verbatim (no closed-set rejection). Pure free-text
 * mode (no options) keeps the original textarea behaviour unchanged.
 */
export default function CheckpointRow({
  item,
  onResolved,
}: {
  item: PendingCheckpoint;
  onResolved: () => void;
}) {
  const t = useTranslations("decisions");
  const offered: string[] | null = item.options && item.options.length > 0 ? item.options : null;
  const hasOptions = offered !== null;

  // In options mode the radio drives `selected`; "Other" reveals `otherText`.
  // In free-text mode only `answer` is used.
  const [selected, setSelected] = useState<string | null>(null);
  const [otherText, setOtherText] = useState("");
  const [answer, setAnswer] = useState("");
  const [state, setState] = useState<RowState>("idle");

  const answerId = useId();
  const otherId = useId();
  const optionsGroupName = useId();

  const otherActive = hasOptions && selected === OTHER_SENTINEL;
  const otherTrimmed = otherText.trim();
  const freeTrimmed = answer.trim();
  const working = state === "working";

  // Resolve the verbatim string that will be POSTed.
  const payload = (() => {
    if (!hasOptions) return freeTrimmed;
    if (selected === OTHER_SENTINEL) return otherTrimmed;
    return selected ?? "";
  })();
  const submitDisabled = working || payload.length === 0;

  async function submit() {
    if (submitDisabled) return;
    setState("working");
    try {
      await resolveCheckpoint(item.checkpointId, payload);
      onResolved();
    } catch {
      setState("error");
    }
  }

  return (
    <li className="decisions-row decisions-row--decision">
      <span className="decisions-row__main">
        <span className="decisions-row__q">{item.question}</span>
        <span className="decisions-row__meta">
          <span className="decisions-chip decisions-chip--decision">{t("kindDecision")}</span>
          <span className="decisions-row__time">{relativeTime(item.createdAt, t)}</span>
        </span>
      </span>
      {item.rationale ? <p className="decisions-row__rationale">{item.rationale}</p> : null}
      {hasOptions && offered !== null ? (
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
        </>
      )}
      <span className="decisions-row__actions">
        {state === "error" && (
          <span className="decisions-row__error" aria-live="polite">
            {t("resolveError")}
          </span>
        )}
        <button
          type="button"
          className="decisions-row__primary"
          onClick={submit}
          disabled={submitDisabled}
        >
          {working ? t("working") : t("answer")}
        </button>
      </span>
    </li>
  );
}
