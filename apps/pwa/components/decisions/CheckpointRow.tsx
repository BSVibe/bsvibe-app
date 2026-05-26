"use client";

import { resolveCheckpoint } from "@/lib/api/checkpoints";
import type { PendingCheckpoint } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";
import { relativeTime } from "./relative-time";

type RowState = "idle" | "working" | "error";

/**
 * One paused-run checkpoint row in the unified Pending list. The agent is
 * blocked on a question (`item.question`); the founder's answer resumes the
 * run via POST /api/v1/checkpoints/{id}/resolve ({ answer }, non-empty — the
 * backend ResolveRequest requires min_length=1, so the submit stays disabled
 * until there is real text).
 *
 * B11a — when the work LLM offered concrete choices (`item.options` non-empty),
 * render them as a single-select (radio group) so the founder picks one
 * instead of free-typing. The backend then validates the answer is one of the
 * offered strings (400 otherwise). Free-text mode (no options) keeps the
 * existing textarea behaviour unchanged.
 *
 * Resolves inline; a failed call keeps the row actionable with a calm message.
 * On success the container re-reads so the resolved item leaves Pending.
 */
export default function CheckpointRow({
  item,
  onResolved,
}: {
  item: PendingCheckpoint;
  onResolved: () => void;
}) {
  const t = useTranslations("decisions");
  const [answer, setAnswer] = useState("");
  const [state, setState] = useState<RowState>("idle");
  const answerId = useId();
  const optionsGroupName = useId();

  const offered: string[] | null = item.options && item.options.length > 0 ? item.options : null;
  const hasOptions = offered !== null;

  const trimmed = answer.trim();
  const working = state === "working";
  const submitDisabled =
    working || (offered !== null ? !offered.includes(answer) : trimmed.length === 0);

  async function submit() {
    if (submitDisabled) return;
    setState("working");
    try {
      // In options mode we POST the chosen option verbatim (no trim — the
      // founder selected it from a fixed list, and the backend matches by
      // strict string equality). In free-text mode we post the trimmed answer.
      const payload = hasOptions ? answer : trimmed;
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
        // Single-select via a radio group. role=radiogroup with aria-label is
        // the founder-facing "Your answer" prompt; each radio carries the
        // accessible name of the offered option so tests / screen readers can
        // address them by label.
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
                  checked={answer === opt}
                  disabled={working}
                  onChange={() => setAnswer(opt)}
                />
                <span>{opt}</span>
              </label>
            );
          })}
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
