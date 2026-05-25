"use client";

import { resolveCheckpoint } from "@/lib/api/checkpoints";
import type { PendingCheckpoint } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";
import { relativeTime } from "./relative-time";

type RowState = "idle" | "working" | "error";

/**
 * One paused-run checkpoint row in the unified Pending list. The agent is
 * blocked on a question (`item.question`); the founder's typed answer resumes
 * the run via POST /api/v1/checkpoints/{id}/resolve ({ answer }, non-empty —
 * the backend ResolveRequest requires min_length=1, so the submit stays
 * disabled until there is real text). Resolves inline; a failed call keeps the
 * row actionable with a calm message. On success the container re-reads so the
 * resolved item leaves Pending.
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

  const trimmed = answer.trim();
  const working = state === "working";

  async function submit() {
    if (working || trimmed.length === 0) return;
    setState("working");
    try {
      await resolveCheckpoint(item.checkpointId, trimmed);
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
          onClick={submit}
          disabled={working || trimmed.length === 0}
        >
          {working ? t("working") : t("answer")}
        </button>
      </span>
    </li>
  );
}
