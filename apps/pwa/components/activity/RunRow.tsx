"use client";

import { getRunDeliverables } from "@/lib/api/activity";
import type { ActivityDeliverable, ActivityRun, ArtifactType } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useState } from "react";

/** Per-artifact-type marker (UX §4 — deliverables are polymorphic), matched to
 *  the Brief's "Recently shipped" glyph vocabulary so the two surfaces feel
 *  like one product. */
const ARTIFACT: Record<ArtifactType, { glyph: string; tone: string }> = {
  pr: { glyph: "◆", tone: "pr" },
  doc: { glyph: "▤", tone: "doc" },
  image: { glyph: "▦", tone: "image" },
  slides: { glyph: "▥", tone: "slides" },
  file: { glyph: "▢", tone: "file" },
  email: { glyph: "✉", tone: "email" },
};

type Loaded =
  | { state: "idle" }
  | { state: "loading" }
  | { state: "error" }
  | {
      state: "ready";
      items: ActivityDeliverable[];
    };

/** Calm absolute date ("May 23 · 2:14 PM"); falls back to the raw string when
 *  unparseable. No date library — keeps the bundle quiet. */
function formatWhen(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const day = date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const time = date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return `${day} · ${time}`;
}

/**
 * One run in the Activity list. A calm row showing the product, a plain-language
 * status (the lone status colour comes from `tone`), and when it last moved.
 * Expanding the row lazily fetches that run's delivered artifacts the first time
 * — each with its type marker, the "This is verified" proof verdict, and an
 * external link when the artifact has an addressable landing spot.
 *
 * Read-only: the only interaction is expand/collapse.
 */
export default function RunRow({ run }: { run: ActivityRun }) {
  const [open, setOpen] = useState(false);
  const [loaded, setLoaded] = useState<Loaded>({ state: "idle" });
  const t = useTranslations("activity");

  async function toggle() {
    const next = !open;
    setOpen(next);
    // Lazy-load the first time it expands; keep the result cached after.
    if (next && loaded.state === "idle") {
      setLoaded({ state: "loading" });
      try {
        const items = await getRunDeliverables(run.runId);
        setLoaded({ state: "ready", items });
      } catch {
        setLoaded({ state: "error" });
      }
    }
  }

  const panelId = `run-${run.runId}-deliverables`;

  return (
    <li className="activity-row">
      <button
        type="button"
        className="activity-row__head"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={toggle}
      >
        <span
          className={`activity-row__chevron${open ? " activity-row__chevron--open" : ""}`}
          aria-hidden="true"
        >
          ›
        </span>
        <span className="activity-row__product">{run.productSlug}</span>
        <span className={`activity-row__status activity-row__status--${run.tone}`}>
          {run.statusLabel}
        </span>
        <span className="activity-row__when">{formatWhen(run.updatedAt)}</span>
      </button>

      <Link className="activity-row__open" href={`/runs/${run.runId}`}>
        {t("openRun")}
      </Link>

      {open && (
        <div id={panelId} className="activity-row__panel">
          {loaded.state === "loading" && (
            <p className="activity-row__note" aria-live="polite">
              {t("deliverablesLoading")}
            </p>
          )}
          {loaded.state === "error" && (
            <p className="activity-row__note" aria-live="polite">
              {t("deliverablesError")}
            </p>
          )}
          {loaded.state === "ready" && loaded.items.length === 0 && (
            <p className="activity-row__note">{t("deliverablesEmpty")}</p>
          )}
          {loaded.state === "ready" && loaded.items.length > 0 && (
            <ul className="activity-deliverables">
              {loaded.items.map((d) => {
                const a = ARTIFACT[d.artifactType];
                return (
                  <li key={d.id} className="activity-deliverable">
                    <span
                      className={`activity-deliverable__icon activity-deliverable__icon--${a.tone}`}
                      aria-hidden="true"
                    >
                      {a.glyph}
                    </span>
                    <div className="activity-deliverable__body">
                      <span className="activity-deliverable__title">{d.title}</span>
                      <span className="activity-deliverable__source">{d.source}</span>
                      {d.link && (
                        <a
                          className="activity-deliverable__link"
                          href={d.link}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          {t("openArtifact")}
                        </a>
                      )}
                    </div>
                    <span className="activity-deliverable__verdict">{d.verdict}</span>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </li>
  );
}
