"use client";

import { getKnowledgeGraph } from "@/lib/api/knowledge";
import type { KnowledgeGraph } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import dynamic from "next/dynamic";
import { useEffect, useState } from "react";

/**
 * The Knowledge surface — the founder's calm, read-only window into what the AI
 * has learned. Following BSage's knowledge graph frontend as-is (founder
 * directive), the surface IS the graph: a force-directed view of concepts +
 * how they relate, with a click→detail side panel. There is no longer a
 * separate "What I know" list or a "Recently observed" section (removed —
 * noise; the system learns from everything).
 *
 * This component owns the graph read (`GET /api/v1/inside/graph`) and its calm
 * states (loading / empty / error), then hands the data to the ported
 * `KnowledgeGraph` view. That view is client-only (`react-force-graph-2d`
 * reaches for `window`/canvas at module scope), so it loads via `next/dynamic`
 * with `ssr: false` — the SSR pass + static build never touch it.
 */
const KnowledgeGraphView = dynamic(() => import("./KnowledgeGraphView"), { ssr: false });

type GraphState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; graph: KnowledgeGraph };

export default function Knowledge() {
  const t = useTranslations("knowledge");
  const [state, setState] = useState<GraphState>({ status: "loading" });

  useEffect(() => {
    let active = true;
    getKnowledgeGraph()
      .then((graph) => {
        if (active) setState({ status: "ready", graph });
      })
      .catch(() => {
        if (active) setState({ status: "error" });
      });
    return () => {
      active = false;
    };
  }, []);

  return (
    <div className="inside inside--graph">
      <h1 className="inside__heading">{t("heading")}</h1>
      <p className="inside__lede">{t("lede")}</p>

      {state.status === "loading" && (
        <p className="inside__loading-note" aria-live="polite">
          {t("graphLoading")}
        </p>
      )}

      {state.status === "error" && (
        <p className="knowledge-graph__note" aria-live="polite">
          {t("graphError")}
        </p>
      )}

      {state.status === "ready" &&
        (state.graph.nodes.length === 0 ? (
          <div className="knowledge-graph__empty" data-testid="knowledge-graph-empty">
            <p className="knowledge-graph__empty-line">{t("graphEmptyLine")}</p>
            <p className="knowledge-graph__empty-sub">{t("graphEmptySub")}</p>
          </div>
        ) : (
          <KnowledgeGraphView graph={state.graph} />
        ))}
    </div>
  );
}
