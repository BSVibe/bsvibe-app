"use client";

import { listSkills } from "@/lib/api/skills";
import type { Skill } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useState } from "react";

/**
 * The Skills Library surface (the left-rail / mobile "Skills" route). The
 * founder's read-only window into the skills loaded for the workspace — a card
 * per skill (name + description + a quiet metadata hint), each linking to its
 * viewer (`/skills/[name]`) for the full manifest.
 *
 * Read-only by design: the backend skills API has NO write path (skill MD files
 * are file-system based — see backend/api/v1/skills.py). The "New skill"
 * affordance is therefore a clearly DISABLED honest stub with a "coming soon"
 * hint, matching how other tabs surface not-yet-built actions.
 *
 * States: a quiet loading note; a calm "No skills yet" empty state for a fresh
 * workspace; a calm inline note (never a blank page or a crash) when the read
 * fails; otherwise the list of skill cards.
 */
type Loaded = { state: "loading" } | { state: "error" } | { state: "ready"; skills: Skill[] };

export default function SkillsLibrary() {
  const [loaded, setLoaded] = useState<Loaded>({ state: "loading" });
  const t = useTranslations("skills");

  useEffect(() => {
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
  }, []);

  return (
    <div className="skills">
      <header className="skills__head">
        <div>
          <h1 className="skills__heading">{t("heading")}</h1>
          <p className="skills__lede">{t("lede")}</p>
        </div>
        {/* No write API — authoring is file-system based. Honest disabled stub. */}
        <button type="button" className="skills__new" disabled title={t("editComingSoon")}>
          {t("newSkill")}
        </button>
      </header>

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
