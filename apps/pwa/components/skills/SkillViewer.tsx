"use client";

import { ApiError } from "@/lib/api/client";
import { getSkill } from "@/lib/api/skills";
import type { Skill } from "@/lib/api/types";
import Link from "next/link";
import { useEffect, useState } from "react";

/**
 * The Skill viewer surface (`/skills/[name]`) — a focused read-only window into
 * one skill's manifest: its name, version, description, author, the tools it is
 * allowed to use, and whether it carries a system prompt.
 *
 * Read-only by design — the backend skills API has NO write path (skill MD
 * files are file-system based; the markdown body itself is not even returned
 * over the wire — only the `has_system_prompt` flag). The "Edit" affordance is
 * therefore a clearly DISABLED honest stub with a "coming soon" hint.
 *
 * States: a quiet loading note; a calm not-found state (an unknown skill 404s →
 * not-found, with a way back to the library); a calm inline error (never a
 * blank page) on any other failure; otherwise the manifest.
 */
type Loaded =
  | { state: "loading" }
  | { state: "error" }
  | { state: "not-found" }
  | { state: "ready"; skill: Skill };

export default function SkillViewer({ name }: { name: string }) {
  const [loaded, setLoaded] = useState<Loaded>({ state: "loading" });

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
        ‹ Skills
      </Link>

      {loaded.state === "loading" && (
        <p className="skill__loading-note" aria-busy="true">
          Looking at this skill…
        </p>
      )}

      {loaded.state === "not-found" && (
        <section className="skills-empty" aria-label="Skill">
          <p className="skills-empty__line">I don&rsquo;t know that skill.</p>
          <p className="skills-empty__sub">
            It may have been renamed or removed. <Link href="/skills">Back to Skills</Link>.
          </p>
        </section>
      )}

      {loaded.state === "error" && (
        <section className="skills-empty" aria-label="Skill">
          <p className="skills-empty__line">Couldn&rsquo;t load this skill just now.</p>
          <p className="skills-empty__sub">Try again in a moment.</p>
        </section>
      )}

      {loaded.state === "ready" && (
        <article className="skill-detail">
          <header className="skill-detail__head">
            <div>
              <h1 className="skill-detail__name">{loaded.skill.name}</h1>
              <p className="skill-detail__meta">
                <span>v{loaded.skill.version}</span>
                {loaded.skill.author && <span> · by {loaded.skill.author}</span>}
                {loaded.skill.model && <span> · {loaded.skill.model}</span>}
              </p>
            </div>
            {/* No write API — authoring is file-system based. Honest disabled stub. */}
            <button
              type="button"
              className="skill-detail__edit"
              disabled
              title="Editing skills is coming soon"
            >
              Edit
            </button>
          </header>

          <p className="skill-detail__desc">{loaded.skill.description}</p>

          {loaded.skill.allowed_tools.length > 0 && (
            <section className="skill-detail__block" aria-label="Allowed tools">
              <h2 className="section-label">Allowed tools</h2>
              <ul className="skill-tags">
                {loaded.skill.allowed_tools.map((tool) => (
                  <li key={tool} className="skill-tag">
                    {tool}
                  </li>
                ))}
              </ul>
            </section>
          )}

          <p className="skill-detail__prompt-note">
            {loaded.skill.has_system_prompt
              ? "This skill carries a system prompt. The prompt text lives in the skill file and isn’t shown here."
              : "This skill has no system prompt."}
          </p>
        </article>
      )}
    </div>
  );
}
