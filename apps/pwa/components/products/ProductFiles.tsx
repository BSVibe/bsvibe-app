"use client";

import {
  getProductFileContent as realGetContent,
  listProductFiles as realListFiles,
} from "@/lib/api/products";
import type { FileTreeEntry, ProductFileContent } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";

/**
 * "Files" — a lazy file-tree browser over the product's git `main`. Replaces
 * the old flat per-deliverable artifact_refs list (which only showed the files
 * a single run touched and didn't scale to a real repo). Each directory's
 * children are fetched on demand from `GET /products/{id}/files?path=` so a
 * large repo stays cheap to browse; selecting a file fetches its content from
 * `GET /products/{id}/files/content?path=`. List left, content right.
 *
 * The list/content clients are injected so the surface is unit-testable.
 */
type ListState =
  | { state: "loading" }
  | { state: "error" }
  | { state: "ready"; entries: FileTreeEntry[] };

type ContentState =
  | { state: "idle" }
  | { state: "loading" }
  | { state: "error" }
  | { state: "ready"; content: ProductFileContent };

export default function ProductFiles({
  productId,
  listFiles = realListFiles,
  getContent = realGetContent,
}: {
  productId: string;
  listFiles?: (productId: string, path?: string) => Promise<FileTreeEntry[]>;
  getContent?: (productId: string, path: string) => Promise<ProductFileContent>;
}) {
  const t = useTranslations("products");
  const [root, setRoot] = useState<ListState>({ state: "loading" });
  // Cached children per expanded directory path.
  const [children, setChildren] = useState<Record<string, FileTreeEntry[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [loadingDirs, setLoadingDirs] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState<ContentState>({ state: "idle" });

  // Load the root level on mount / product change. Reset all tree state so a
  // product switch never shows the previous product's tree.
  useEffect(() => {
    let active = true;
    setRoot({ state: "loading" });
    setChildren({});
    setExpanded(new Set());
    setSelected(null);
    setContent({ state: "idle" });
    listFiles(productId)
      .then((entries) => active && setRoot({ state: "ready", entries }))
      .catch(() => active && setRoot({ state: "error" }));
    return () => {
      active = false;
    };
  }, [productId, listFiles]);

  const toggleDir = useCallback(
    async (path: string) => {
      const willExpand = !expanded.has(path);
      setExpanded((prev) => {
        const next = new Set(prev);
        if (willExpand) {
          next.add(path);
        } else {
          next.delete(path);
        }
        return next;
      });
      // Lazy-load children the first time the folder is opened.
      if (willExpand && children[path] === undefined) {
        setLoadingDirs((s) => new Set(s).add(path));
        try {
          const entries = await listFiles(productId, path);
          setChildren((c) => ({ ...c, [path]: entries }));
        } catch {
          setChildren((c) => ({ ...c, [path]: [] }));
        } finally {
          setLoadingDirs((s) => {
            const n = new Set(s);
            n.delete(path);
            return n;
          });
        }
      }
    },
    [expanded, children, listFiles, productId],
  );

  const selectFile = useCallback(
    (path: string) => {
      setSelected(path);
      setContent({ state: "loading" });
      getContent(productId, path)
        .then((c) => setContent({ state: "ready", content: c }))
        .catch(() => setContent({ state: "error" }));
    },
    [getContent, productId],
  );

  function renderNodes(entries: FileTreeEntry[], depth: number) {
    return (
      <ul className="product-files__tree">
        {entries.map((entry) => (
          <li key={entry.path}>
            {entry.kind === "dir" ? (
              <>
                <button
                  type="button"
                  className="product-files__node product-files__node--dir"
                  style={{ paddingLeft: `${depth * 14 + 8}px` }}
                  aria-expanded={expanded.has(entry.path)}
                  onClick={() => toggleDir(entry.path)}
                >
                  <span className="product-files__caret" aria-hidden="true">
                    {expanded.has(entry.path) ? "▾" : "▸"}
                  </span>
                  {entry.name}
                </button>
                {expanded.has(entry.path) &&
                  (loadingDirs.has(entry.path) ? (
                    <p
                      className="product-files__hint"
                      style={{ paddingLeft: `${(depth + 1) * 14 + 8}px` }}
                      aria-busy="true"
                    >
                      {t("fileLoading")}
                    </p>
                  ) : (
                    renderNodes(children[entry.path] ?? [], depth + 1)
                  ))}
              </>
            ) : (
              <button
                type="button"
                className={`product-files__node product-files__node--file${
                  selected === entry.path ? " product-files__node--active" : ""
                }`}
                style={{ paddingLeft: `${depth * 14 + 22}px` }}
                onClick={() => selectFile(entry.path)}
                title={entry.path}
              >
                {entry.name}
              </button>
            )}
          </li>
        ))}
      </ul>
    );
  }

  return (
    <section className="product-files" aria-label={t("files")}>
      <h2 className="section-label">{t("files")}</h2>

      {root.state === "loading" && (
        <p className="product-files__empty" aria-busy="true">
          {t("fileLoading")}
        </p>
      )}
      {root.state === "error" && <p className="product-files__empty">{t("fileError")}</p>}
      {root.state === "ready" && root.entries.length === 0 && (
        <p className="product-files__empty">{t("filesEmpty")}</p>
      )}

      {root.state === "ready" && root.entries.length > 0 && (
        <div className="product-files__split">
          <div className="product-files__list">{renderNodes(root.entries, 0)}</div>

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
                  <span className="product-files__file-path">{content.content.path}</span>
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
      )}
    </section>
  );
}
