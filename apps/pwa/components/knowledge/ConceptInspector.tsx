"use client";

import { ApiError } from "@/lib/api/client";
import { getConceptDetail } from "@/lib/api/knowledge";
import type { ConceptDetail } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

/**
 * The read-only concept inspector — the detail drawer behind a clicked concept
 * (a graph node OR a "What I know" list row), sourced from
 * `GET /api/v1/inside/concepts/{id}`.
 *
 * It shows the concept's name + aliases, its **related concepts** (graph
 * neighbours, each a button that pivots the inspector onto it via `onPivot`),
 * and its **source observations** (the garden notes that reference it — its
 * origin/usage). A 404 degrades to a calm "I don't know that concept" note; any
 * other failure to a calm inline error — never a crash or a blank drawer.
 *
 * Read-only by design. Stitch's Edit / Retract affordances map to
 * canonicalization deprecate/edit actions that have no v1 endpoint yet, so they
 * render DISABLED with a "coming soon" hint (mirrors the Skill viewer's deferred
 * Edit affordance) rather than being hidden — the founder sees the intent.
 */
type DetailState =
  | { status: "loading" }
  | { status: "not-found" }
  | { status: "error" }
  | { status: "ready"; detail: ConceptDetail };

export default function ConceptInspector({
  conceptId,
  onClose,
  onPivot,
}: {
  conceptId: string;
  onClose: () => void;
  onPivot: (id: string) => void;
}) {
  const t = useTranslations("knowledge");
  const [state, setState] = useState<DetailState>({ status: "loading" });

  useEffect(() => {
    let active = true;
    setState({ status: "loading" });
    getConceptDetail(conceptId)
      .then((detail) => {
        if (active) setState({ status: "ready", detail });
      })
      .catch((err) => {
        if (!active) return;
        if (err instanceof ApiError && err.status === 404) {
          setState({ status: "not-found" });
        } else {
          setState({ status: "error" });
        }
      });
    return () => {
      active = false;
    };
  }, [conceptId]);

  return (
    <aside className="concept-inspector" aria-label={t("inspectorLabel")}>
      <header className="concept-inspector__head">
        <h2 className="concept-inspector__name">
          {state.status === "ready" ? state.detail.name : t("inspectorLabel")}
        </h2>
        <button
          type="button"
          className="concept-inspector__close"
          onClick={onClose}
          aria-label={t("inspectorClose")}
        >
          ×
        </button>
      </header>

      {state.status === "loading" && (
        <p className="concept-inspector__note" aria-live="polite">
          {t("inspectorLoading")}
        </p>
      )}

      {state.status === "not-found" && (
        <p className="concept-inspector__note" aria-live="polite">
          {t("inspectorNotFound")}
        </p>
      )}

      {state.status === "error" && (
        <p className="concept-inspector__note" aria-live="polite">
          {t("inspectorError")}
        </p>
      )}

      {state.status === "ready" && (
        <div className="concept-inspector__body">
          {state.detail.aliases.length > 0 && (
            <section className="concept-inspector__block">
              <h3 className="concept-inspector__block-label">{t("inspectorAliases")}</h3>
              <ul className="inside-tags">
                {state.detail.aliases.map((alias) => (
                  <li key={alias} className="inside-tag">
                    {alias}
                  </li>
                ))}
              </ul>
            </section>
          )}

          <section className="concept-inspector__block">
            <h3 className="concept-inspector__block-label">{t("inspectorRelated")}</h3>
            {state.detail.related.length === 0 ? (
              <p className="concept-inspector__empty">{t("inspectorRelatedEmpty")}</p>
            ) : (
              <ul className="concept-inspector__related">
                {state.detail.related.map((rel) => (
                  <li key={rel.id}>
                    <button
                      type="button"
                      className="concept-inspector__chip"
                      onClick={() => onPivot(rel.id)}
                    >
                      <span className="concept-inspector__chip-name">{rel.name}</span>
                      <span className="concept-inspector__chip-weight">
                        {t("relatedWeight", { count: rel.weight })}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="concept-inspector__block">
            <h3 className="concept-inspector__block-label">{t("inspectorOrigin")}</h3>
            {state.detail.observations.length === 0 ? (
              <p className="concept-inspector__empty">{t("inspectorOriginEmpty")}</p>
            ) : (
              <ul className="concept-inspector__obs">
                {state.detail.observations.map((obs) => (
                  <li key={obs.id} className="concept-inspector__obs-row">
                    <div className="concept-inspector__obs-head">
                      <span className="concept-inspector__obs-title">{obs.title}</span>
                      {obs.captured_at && (
                        <span className="concept-inspector__obs-date">{obs.captured_at}</span>
                      )}
                    </div>
                    {obs.excerpt && <p className="concept-inspector__obs-excerpt">{obs.excerpt}</p>}
                  </li>
                ))}
              </ul>
            )}
          </section>

          {/* Deferred — Edit / Retract map to canonicalization deprecate/edit
              actions with no v1 endpoint yet. Shown DISABLED so the intent is
              visible (mirrors the Skill viewer's deferred Edit affordance). */}
          <footer className="concept-inspector__actions">
            <button
              type="button"
              className="concept-inspector__action"
              disabled
              title={t("inspectorActionSoon")}
            >
              {t("inspectorEdit")}
            </button>
            <button
              type="button"
              className="concept-inspector__action concept-inspector__action--danger"
              disabled
              title={t("inspectorActionSoon")}
            >
              {t("inspectorRetract")}
            </button>
          </footer>
        </div>
      )}
    </aside>
  );
}
