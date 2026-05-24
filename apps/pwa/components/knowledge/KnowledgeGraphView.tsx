"use client";

import { getKnowledgeGraph } from "@/lib/api/knowledge";
import type { KnowledgeGraph } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import dynamic from "next/dynamic";
import { useEffect, useState } from "react";

/**
 * The Knowledge surface's primary view: a force-directed graph of the
 * workspace's knowledge (concepts + how they relate), sourced from
 * `GET /api/v1/inside/graph`.
 *
 * The canvas itself (`react-force-graph-2d`) reaches for `window`/canvas at
 * module scope, so it is loaded via `next/dynamic` with `ssr: false` — the SSR
 * pass + static build never touch it. While the chunk + the graph data load we
 * show a calm note; an empty/sparse graph degrades to a calm empty state
 * ("No connections yet…"); a failed read shows a calm inline error — never a
 * crash or a blank page.
 */
const ForceGraphCanvas = dynamic(() => import("./ForceGraphCanvas"), {
  ssr: false,
});

type GraphState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; graph: KnowledgeGraph };

export default function KnowledgeGraphView({
  filter = "",
  onNodeClick,
}: {
  /** Search needle propagated to the canvas (matching nodes stay vivid). */
  filter?: string;
  /** Fired with a node id when a graph node is tapped (opens the inspector). */
  onNodeClick?: (id: string) => void;
} = {}) {
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
    <section className="knowledge-graph" aria-label={t("graphLabel")}>
      <h2 className="section-label">{t("graphLabel")}</h2>
      {state.status === "loading" && (
        <p className="knowledge-graph__note" aria-live="polite">
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
          <div className="knowledge-graph__canvas" data-testid="knowledge-graph-canvas">
            <ForceGraphCanvas graph={state.graph} filter={filter} onNodeClick={onNodeClick} />
          </div>
        ))}
    </section>
  );
}
