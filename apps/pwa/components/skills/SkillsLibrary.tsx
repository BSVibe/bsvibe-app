"use client";

import { ApiError } from "@/lib/api/client";
import { createSkill, listSkills } from "@/lib/api/skills";
import type { Skill } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { type FormEvent, useEffect, useState } from "react";

/**
 * The Skills Library surface (the left-rail / mobile "Skills" route). The
 * founder's window into the skills loaded for the workspace — a card per skill
 * (name + description + a quiet metadata hint), each linking to its viewer
 * (`/skills/[name]`) for the full manifest.
 *
 * The "New skill" affordance opens an inline create form (Name + Summary +
 * System prompt). On submit it POSTs `createSkill`; on the 201 the list refreshes
 * to include the new skill and the form closes. A duplicate (409) — or any
 * failure — shows a calm inline note and the form stays open + usable. CREATE
 * only: there is no edit/delete affordance yet (backend has no update/delete).
 *
 * States: a quiet loading note; a calm "No skills yet" empty state for a fresh
 * workspace; a calm inline note (never a blank page or a crash) when the read
 * fails; otherwise the list of skill cards.
 */
type Loaded = { state: "loading" } | { state: "error" } | { state: "ready"; skills: Skill[] };

export default function SkillsLibrary() {
  const [loaded, setLoaded] = useState<Loaded>({ state: "loading" });
  const [creating, setCreating] = useState(false);
  const t = useTranslations("skills");

  function refresh() {
    let active = true;
    setLoaded({ state: "loading" });
    listSkills()
      .then((skills) => {
        if (active) setLoaded({ state: "ready", skills });
      })
      .catch(() => {
        if (active) setLoaded({ state: "error" });
      });
    return () => {
      active = false;
    };
  }

  // biome-ignore lint/correctness/useExhaustiveDependencies: load once on mount.
  useEffect(() => refresh(), []);

  return (
    <div className="skills">
      <header className="skills__head">
        <div>
          <h1 className="skills__heading">{t("heading")}</h1>
          <p className="skills__lede">{t("lede")}</p>
        </div>
        <button
          type="button"
          className="skills__new"
          onClick={() => setCreating(true)}
          disabled={creating}
        >
          {t("newSkill")}
        </button>
      </header>

      {creating && (
        <NewSkillForm
          onCancel={() => setCreating(false)}
          onCreated={() => {
            setCreating(false);
            refresh();
          }}
        />
      )}

      {loaded.state === "loading" && (
        <p className="skills__loading-note" aria-busy="true">
          {t("loadingNote")}
        </p>
      )}

      {loaded.state === "error" && (
        <section className="skills-empty" aria-label={t("heading")}>
          <p className="skills-empty__line">{t("errorLine")}</p>
          <p className="skills-empty__sub">{t("errorSub")}</p>
        </section>
      )}

      {loaded.state === "ready" &&
        (loaded.skills.length === 0 ? (
          <section className="skills-empty" aria-label={t("heading")}>
            <p className="skills-empty__line">{t("emptyLine")}</p>
            <p className="skills-empty__sub">{t("emptySub")}</p>
          </section>
        ) : (
          <ul className="skills-list">
            {loaded.skills.map((skill) => (
              <li key={skill.name} className="skills-card">
                <Link
                  className="skills-card__link"
                  href={`/skills/${encodeURIComponent(skill.name)}`}
                >
                  <span className="skills-card__name">{skill.name}</span>
                  <p className="skills-card__desc">{skill.description}</p>
                  {skill.has_system_prompt && (
                    <span className="skills-card__hint">{t("hasSystemPrompt")}</span>
                  )}
                </Link>
              </li>
            ))}
          </ul>
        ))}
    </div>
  );
}

/**
 * Inline create form for a new skill. Validates the three required fields
 * locally (never POSTs a blank field), then calls `createSkill`. On success it
 * tells the parent to refresh + close; on a 409 (or any failure) it shows a calm
 * inline note and keeps the form open + usable.
 */
function NewSkillForm({
  onCancel,
  onCreated,
}: {
  onCancel: () => void;
  onCreated: () => void;
}) {
  const t = useTranslations("skills");
  const [name, setName] = useState("");
  const [summary, setSummary] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    if (!name.trim() || !summary.trim() || !systemPrompt.trim()) {
      setError(t("createValidation"));
      return;
    }
    setSubmitting(true);
    try {
      await createSkill({
        name: name.trim(),
        summary: summary.trim(),
        system_prompt: systemPrompt.trim(),
      });
      onCreated();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setError(t("createConflict"));
      } else {
        setError(t("createError"));
      }
      setSubmitting(false);
    }
  }

  return (
    <form className="skills-form" onSubmit={onSubmit} aria-label={t("createHeading")}>
      <h2 className="skills-form__heading">{t("createHeading")}</h2>

      <label className="skills-form__field">
        <span className="skills-form__label">{t("createNameLabel")}</span>
        <input
          className="skills-form__input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={t("createNamePlaceholder")}
        />
      </label>

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
          rows={6}
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
          {t("createSubmit")}
        </button>
      </div>
    </form>
  );
}
