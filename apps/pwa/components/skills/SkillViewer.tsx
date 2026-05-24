"use client";

import SkillEditor from "@/components/skills/SkillEditor";
import { ApiError } from "@/lib/api/client";
import { getSkill } from "@/lib/api/skills";
import type { Skill } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useState } from "react";

/**
 * The Skill viewer surface (`/skills/[name]`) — a focused window into one skill's
 * manifest: its name, version, description, author, the tools it is allowed to
 * use, and its system-prompt body.
 *
 * The "Edit" affordance opens an inline editor (`SkillEditor`) for the editable
 * body fields (`summary` + `system_prompt`); on save it PATCHes and the viewer
 * returns to the read view with the updated manifest. The slug / name is
 * immutable (no rename — that would be a file rename, deferred).
 *
 * States: a quiet loading note; a calm not-found state (an unknown skill 404s →
 * not-found, with a way back to the library); a calm inline error (never a
 * blank page) on any other failure; otherwise the manifest (or the editor).
 */
type Loaded =
  | { state: "loading" }
  | { state: "error" }
  | { state: "not-found" }
  | { state: "ready"; skill: Skill };

export default function SkillViewer({ name }: { name: string }) {
  const [loaded, setLoaded] = useState<Loaded>({ state: "loading" });
  const [editing, setEditing] = useState(false);
  const t = useTranslations("skills");

  useEffect(() => {
    let active = true;
    setLoaded({ state: "loading" });
    getSkill(name)
      .then((skill) => {
        if (active) setLoaded({ state: "ready", skill });
      })
      .catch((err: unknown) => {
        if (!active) return;
        // An unknown skill is a calm not-found, NOT an error wall.
        if (err instanceof ApiError && err.status === 404) {
          setLoaded({ state: "not-found" });
        } else {
          setLoaded({ state: "error" });
        }
      });
    return () => {
      active = false;
    };
  }, [name]);

  return (
    <div className="skill">
      <Link className="skill__back" href="/skills">
        {t("back")}
      </Link>

      {loaded.state === "loading" && (
        <p className="skill__loading-note" aria-busy="true">
          {t("viewerLoadingNote")}
        </p>
      )}

      {loaded.state === "not-found" && (
        <section className="skills-empty" aria-label={t("skillRegion")}>
          <p className="skills-empty__line">{t("notFoundLine")}</p>
          <p className="skills-empty__sub">
            {t("notFoundSubPrefix")}
            <Link href="/skills">{t("backToSkills")}</Link>
            {t("notFoundSubSuffix")}
          </p>
        </section>
      )}

      {loaded.state === "error" && (
        <section className="skills-empty" aria-label={t("skillRegion")}>
          <p className="skills-empty__line">{t("viewerErrorLine")}</p>
          <p className="skills-empty__sub">{t("viewerErrorSub")}</p>
        </section>
      )}

      {loaded.state === "ready" &&
        (editing ? (
          <SkillEditor
            skill={loaded.skill}
            onCancel={() => setEditing(false)}
            onSaved={(updated) => {
              setLoaded({ state: "ready", skill: updated });
              setEditing(false);
            }}
          />
        ) : (
          <article className="skill-detail">
            <header className="skill-detail__head">
              <div>
                <h1 className="skill-detail__name">{loaded.skill.name}</h1>
                <p className="skill-detail__meta">
                  <span>{t("version", { version: loaded.skill.version })}</span>
                  {loaded.skill.author && (
                    <span>{t("byAuthor", { author: loaded.skill.author })}</span>
                  )}
                  {loaded.skill.model && (
                    <span>{t("withModel", { model: loaded.skill.model })}</span>
                  )}
                </p>
              </div>
              <button type="button" className="skill-detail__edit" onClick={() => setEditing(true)}>
                {t("edit")}
              </button>
            </header>

            <p className="skill-detail__desc">{loaded.skill.description}</p>

            {loaded.skill.allowed_tools.length > 0 && (
              <section className="skill-detail__block" aria-label={t("allowedTools")}>
                <h2 className="section-label">{t("allowedTools")}</h2>
                <ul className="skill-tags">
                  {loaded.skill.allowed_tools.map((tool) => (
                    <li key={tool} className="skill-tag">
                      {tool}
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {loaded.skill.has_system_prompt ? (
              <section className="skill-detail__block" aria-label={t("createSystemPromptLabel")}>
                <h2 className="section-label">{t("createSystemPromptLabel")}</h2>
                <pre className="skill-detail__prompt">{loaded.skill.system_prompt}</pre>
              </section>
            ) : (
              <p className="skill-detail__prompt-note">{t("systemPromptNo")}</p>
            )}
          </article>
        ))}
    </div>
  );
}
