"use client";

import { getDeliverableArtifact } from "@/lib/api/deliverables";
import type { ArtifactContent, ProductFile } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

/**
 * "Files" — an inline viewer for the files this product has produced. Every
 * shipped deliverable's `artifact_refs` are flattened into one list (grouped by
 * the producing deliverable); selecting a file fetches its content from the
 * EXISTING whitelisted artifact endpoint (`GET /deliverables/{id}/artifacts/
 * {ref}`) and shows it read-only — no new backend, the same source the Delivery
 * Report reads. Modeled on BSNexus's WorkspaceIndex (list left, content right),
 * but flat over artifact refs rather than a directory tree.
 *
 * A calm empty line when the product has produced nothing. A per-file fetch
 * failure (404 / cleaned run dir / oversize) degrades to a calm note rather
 * than blanking the surface.
 */
type ContentState =
  | { state: "idle" }
  | { state: "loading" }
  | { state: "error" }
  | { state: "ready"; content: ArtifactContent };

/** The leaf name of an artifact ref, for a compact row label. */
function leafOf(ref: string): string {
  return ref.split("/").pop() || ref;
}

export default function ProductFiles({ files }: { files: ProductFile[] }) {
  const t = useTranslations("products");
  const [selected, setSelected] = useState<ProductFile | null>(null);
  const [content, setContent] = useState<ContentState>({ state: "idle" });

  useEffect(() => {
    if (!selected) {
      setContent({ state: "idle" });
      return;
    }
    let active = true;
    setContent({ state: "loading" });
    getDeliverableArtifact(selected.deliverableId, selected.ref)
      .then((c) => active && setContent({ state: "ready", content: c }))
      .catch(() => active && setContent({ state: "error" }));
    return () => {
      active = false;
    };
  }, [selected]);

  if (files.length === 0) {
    return (
      <section className="product-files" aria-label={t("files")}>
        <h2 className="section-label">{t("files")}</h2>
        <p className="product-files__empty">{t("filesEmpty")}</p>
      </section>
    );
  }

  // Group the flat file list by its producing deliverable for calm headers.
  const groups = new Map<string, ProductFile[]>();
  for (const f of files) {
    const list = groups.get(f.deliverableTitle) ?? [];
    list.push(f);
    groups.set(f.deliverableTitle, list);
  }

  return (
    <section className="product-files" aria-label={t("files")}>
      <h2 className="section-label">{t("files")}</h2>
      <div className="product-files__split">
        <ul className="product-files__list">
          {[...groups.entries()].map(([title, group]) => (
            <li key={title} className="product-files__group">
              <span className="product-files__group-title">{title}</span>
              <ul className="product-files__refs">
                {group.map((f) => (
                  <li key={f.id}>
                    <button
                      type="button"
                      className={`product-files__ref${
                        selected?.id === f.id ? " product-files__ref--active" : ""
                      }`}
                      onClick={() => setSelected(f)}
                      title={f.ref}
                    >
                      {leafOf(f.ref)}
                    </button>
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>

        <div className="product-files__content">
          {content.state === "idle" && (
            <p className="product-files__hint">{t("filesSelectHint")}</p>
          )}
          {content.state === "loading" && (
            <p className="product-files__hint" aria-busy="true">
              {t("fileLoading")}
            </p>
          )}
          {content.state === "error" && <p className="product-files__hint">{t("fileError")}</p>}
          {content.state === "ready" && (
            <>
              <div className="product-files__file-head">
                <span className="product-files__file-path">{content.content.ref}</span>
              </div>
              {content.content.binary ? (
                <p className="product-files__hint">{t("fileBinary")}</p>
              ) : (
                <>
                  {content.content.truncated && (
                    <p className="product-files__truncated">{t("fileTruncated")}</p>
                  )}
                  <pre className="product-files__pre">{content.content.content}</pre>
                </>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}
