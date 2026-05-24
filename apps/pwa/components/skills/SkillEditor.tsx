"use client";

import { updateSkill } from "@/lib/api/skills";
import type { Skill } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { type FormEvent, useState } from "react";

/**
 * The Skill editor form — edit one skill's editable body fields. The skill
 * `name` (slug) is read-only (a rename would mean a file rename on the backend,
 * deferred); the editable fields are `summary` (the LLM invocation match signal)
 * and the `system_prompt` Markdown body, both prefilled from the loaded skill.
 *
 * Save validates the two required fields locally (never PATCHes a blank field),
 * then calls `updateSkill`; on the 200 it hands the updated skill back to the
 * parent (which returns to the read view). On any failure it shows a calm inline
 * note and keeps the form open + usable. Cancel returns without firing anything.
 *
 * Reuses the create-form (`skills-form__*`) styling for consistency with the
 * New-skill flow. Deferred (no backend fields): Stitch's category / trigger /
 * visibility selectors and the test panel — the skill data model carries none of
 * those, so they are intentionally omitted (no invented columns).
 */
export default function SkillEditor({
  skill,
  onSaved,
  onCancel,
}: {
  skill: Skill;
  onSaved: (updated: Skill) => void;
  onCancel: () => void;
}) {
  const t = useTranslations("skills");
  const [summary, setSummary] = useState(skill.description);
  const [systemPrompt, setSystemPrompt] = useState(skill.system_prompt);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    if (!summary.trim() || !systemPrompt.trim()) {
      setError(t("editValidation"));
      return;
    }
    setSubmitting(true);
    try {
      const updated = await updateSkill(skill.name, {
        summary: summary.trim(),
        system_prompt: systemPrompt.trim(),
      });
      onSaved(updated);
    } catch {
      setError(t("editError"));
      setSubmitting(false);
    }
  }

  return (
    <form className="skills-form" onSubmit={onSubmit} aria-label={t("editHeading")}>
      <h2 className="skills-form__heading">{t("editHeading")}</h2>

      <div className="skills-form__field">
        <span className="skills-form__label">{t("createNameLabel")}</span>
        {/* Name / slug is immutable on update — shown read-only, not an input. */}
        <p className="skills-form__readonly">{skill.name}</p>
      </div>

      <label className="skills-form__field">
        <span className="skills-form__label">{t("createSummaryLabel")}</span>
        <input
          className="skills-form__input"
          value={summary}
          onChange={(e) => setSummary(e.target.value)}
          placeholder={t("createSummaryPlaceholder")}
        />
      </label>

      <label className="skills-form__field">
        <span className="skills-form__label">{t("createSystemPromptLabel")}</span>
        <textarea
          className="skills-form__textarea"
          value={systemPrompt}
          onChange={(e) => setSystemPrompt(e.target.value)}
          placeholder={t("createSystemPromptPlaceholder")}
          rows={10}
        />
      </label>

      {error && (
        <p className="skills-form__error" role="alert">
          {error}
        </p>
      )}

      <div className="skills-form__actions">
        <button type="button" className="skills-form__cancel" onClick={onCancel}>
          {t("createCancel")}
        </button>
        <button type="submit" className="skills-form__submit" disabled={submitting}>
          {t("editSave")}
        </button>
      </div>
    </form>
  );
}
